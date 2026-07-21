package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const (
	// Feed the summarizer at most this many estimated tokens of history text.
	// Must stay under Grok's 500k hard prompt limit.
	grokSummarySourceTokenBudget = 380_000
	// Keep the latest turns verbatim after compact (plus system prefix).
	grokSummaryKeepRecentItems = 4
	// Max summary size we ask the model to produce.
	grokSummaryMaxOutputTokens = 4096
	// Bound how much plain text we send to the summarizer (chars ≈ 2 * tokens).
	grokSummarySourceMaxChars = grokSummarySourceTokenBudget * 2
	// Hard timeout for the side-channel summary call.
	grokSummaryTimeout = 75 * time.Second
	// Marker so nested summary calls never re-enter auto-compact.
	grokSummaryCompactMarker = "__sub2api_grok_summary_compact__"
)

type grokPromptCompactMeta struct {
	Mode           string // noop | summary | hard_drop
	DroppedItems   int
	Summarized     bool
	SummaryChars   int
	BeforeEstTokens int
	AfterEstTokens  int
}

// autoCompactGrokPrompt compresses oversize Grok requests before upstream.
// Prefer summary-style compact (like OpenAI remote compact spirit): summarize
// older turns via the same Grok account, keep system + recent turns.
// Fall back to hard-drop if summary fails or is still over budget.
func (s *OpenAIGatewayService) autoCompactGrokPrompt(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	body []byte,
	model string,
	token string,
	proxyURL string,
) ([]byte, grokPromptCompactMeta, error) {
	meta := grokPromptCompactMeta{Mode: "noop"}
	if len(body) == 0 || account == nil {
		return body, meta, nil
	}
	// Nested summary requests must not compact again.
	if gjson.GetBytes(body, grokSummaryCompactMarker).Bool() {
		return body, meta, nil
	}

	budget := grokPromptTokenBudget(model)
	before := estimateGrokResponsesBodyTokens(body, model)
	meta.BeforeEstTokens = before
	if before <= budget {
		meta.AfterEstTokens = before
		return body, meta, nil
	}

	// Prefer summary compact when we have credentials + upstream client.
	if s != nil && s.httpUpstream != nil && strings.TrimSpace(token) != "" {
		if out, sumMeta, err := s.summaryCompactGrokPrompt(ctx, c, account, body, model, token, proxyURL, budget); err == nil {
			after := estimateGrokResponsesBodyTokens(out, model)
			sumMeta.BeforeEstTokens = before
			sumMeta.AfterEstTokens = after
			if after <= budget {
				return out, sumMeta, nil
			}
			// Summary helped but still over: hard-drop residual.
			hard, dropped, herr := compactGrokResponsesInputIfNeeded(out, model)
			if herr != nil {
				return out, sumMeta, nil
			}
			if dropped > 0 {
				sumMeta.Mode = "summary+hard_drop"
				sumMeta.DroppedItems = dropped
				sumMeta.AfterEstTokens = estimateGrokResponsesBodyTokens(hard, model)
				return hard, sumMeta, nil
			}
			return out, sumMeta, nil
		} else {
			slog.Warn("grok_prompt_summary_compact_failed",
				"account_id", account.ID,
				"model", model,
				"error", err.Error(),
			)
		}
	}

	// Fallback: hard drop oldest items.
	hard, dropped, err := compactGrokResponsesInputIfNeeded(body, model)
	if err != nil {
		return body, meta, err
	}
	meta.Mode = "hard_drop"
	meta.DroppedItems = dropped
	meta.AfterEstTokens = estimateGrokResponsesBodyTokens(hard, model)
	if dropped == 0 {
		meta.Mode = "noop"
	}
	return hard, meta, nil
}

func (s *OpenAIGatewayService) summaryCompactGrokPrompt(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	body []byte,
	model string,
	token string,
	proxyURL string,
	budget int,
) ([]byte, grokPromptCompactMeta, error) {
	meta := grokPromptCompactMeta{Mode: "summary", Summarized: true}

	field, items, keepPrefix, err := grokPromptArrayField(body)
	if err != nil {
		return nil, meta, err
	}
	n := len(items)
	if n <= keepPrefix+grokSummaryKeepRecentItems {
		return nil, meta, fmt.Errorf("not enough items to summarize")
	}

	// Keep: [0:keepPrefix] system + last recent items.
	recentStart := n - grokSummaryKeepRecentItems
	if recentStart < keepPrefix {
		recentStart = keepPrefix
	}
	// Middle to summarize: [keepPrefix:recentStart)
	if recentStart <= keepPrefix {
		return nil, meta, fmt.Errorf("empty history window")
	}
	middle := items[keepPrefix:recentStart]
	historyText := extractGrokItemsPlainText(middle)
	if strings.TrimSpace(historyText) == "" {
		return nil, meta, fmt.Errorf("no extractable text in history")
	}
	historyText = trimGrokHistoryForSummary(historyText, grokSummarySourceMaxChars)

	summary, err := s.callGrokSummaryCompact(ctx, c, account, model, token, proxyURL, historyText)
	if err != nil {
		return nil, meta, err
	}
	summary = strings.TrimSpace(summary)
	if summary == "" {
		return nil, meta, fmt.Errorf("empty summary from upstream")
	}
	meta.SummaryChars = utf8.RuneCountInString(summary)
	meta.DroppedItems = len(middle)

	summaryItem, err := buildGrokSummaryInputItem(field, summary)
	if err != nil {
		return nil, meta, err
	}

	// Rebuild array: system prefix + summary item + recent turns.
	rawParts := make([][]byte, 0, keepPrefix+1+(n-recentStart))
	for _, it := range items[:keepPrefix] {
		rawParts = append(rawParts, []byte(it.Raw))
	}
	rawParts = append(rawParts, summaryItem)
	for _, it := range items[recentStart:] {
		rawParts = append(rawParts, []byte(it.Raw))
	}
	joined := append(append([]byte{'['}, bytes.Join(rawParts, []byte{','})...), ']')
	out, err := sjson.SetRawBytes(body, field, joined)
	if err != nil {
		return nil, meta, err
	}

	// Strip huge instructions if still over budget after summary (rare).
	if estimateGrokResponsesBodyTokens(out, model) > budget {
		if instr := gjson.GetBytes(out, "instructions"); instr.Exists() {
			trimmed := trimRunes(instr.String(), 8000)
			if next, e := sjson.SetBytes(out, "instructions", trimmed); e == nil {
				out = next
			}
		}
	}
	return out, meta, nil
}

func grokPromptArrayField(body []byte) (field string, items []gjson.Result, keepPrefix int, err error) {
	if input := gjson.GetBytes(body, "input"); input.Exists() && input.IsArray() {
		field = "input"
		items = input.Array()
	} else if messages := gjson.GetBytes(body, "messages"); messages.Exists() && messages.IsArray() {
		field = "messages"
		items = messages.Array()
	} else {
		return "", nil, 0, fmt.Errorf("no input/messages array")
	}
	if field == "messages" {
		for keepPrefix < len(items) {
			role := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("role").String()))
			if role != "system" && role != "developer" {
				break
			}
			keepPrefix++
		}
	} else {
		// Responses input: keep leading system/developer message items.
		for keepPrefix < len(items) {
			role := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("role").String()))
			typ := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("type").String()))
			if typ != "" && typ != "message" {
				break
			}
			if role != "system" && role != "developer" {
				break
			}
			keepPrefix++
		}
	}
	return field, items, keepPrefix, nil
}

func extractGrokItemsPlainText(items []gjson.Result) string {
	var b strings.Builder
	for _, it := range items {
		role := strings.TrimSpace(it.Get("role").String())
		typ := strings.TrimSpace(it.Get("type").String())
		if role == "" {
			role = typ
		}
		if role == "" {
			role = "item"
		}
		text := extractGrokItemText(it)
		text = strings.TrimSpace(text)
		if text == "" {
			continue
		}
		// Cap per-item to avoid one giant tool dump dominating.
		if utf8.RuneCountInString(text) > 20_000 {
			text = trimRunes(text, 20_000)
		}
		b.WriteString("[")
		b.WriteString(role)
		b.WriteString("]\n")
		b.WriteString(text)
		b.WriteString("\n\n")
	}
	return b.String()
}

func extractGrokItemText(it gjson.Result) string {
	// content may be string or array of parts.
	content := it.Get("content")
	if content.Type == gjson.String {
		return content.String()
	}
	if content.IsArray() {
		var parts []string
		content.ForEach(func(_, part gjson.Result) bool {
			t := part.Get("type").String()
			switch strings.ToLower(t) {
			case "text", "input_text", "output_text":
				if s := strings.TrimSpace(part.Get("text").String()); s != "" {
					parts = append(parts, s)
				}
			case "input_image", "image_url":
				// Skip image payloads — too large / not useful for text summary.
			default:
				if s := strings.TrimSpace(part.Get("text").String()); s != "" {
					parts = append(parts, s)
				}
			}
			return true
		})
		return strings.Join(parts, "\n")
	}
	// function_call / function_call_output style
	if s := strings.TrimSpace(it.Get("arguments").String()); s != "" {
		return s
	}
	if s := strings.TrimSpace(it.Get("output").String()); s != "" {
		return s
	}
	if s := strings.TrimSpace(it.Get("text").String()); s != "" {
		return s
	}
	return ""
}

func trimGrokHistoryForSummary(text string, maxChars int) string {
	if maxChars <= 0 {
		return ""
	}
	r := []rune(text)
	if len(r) <= maxChars {
		return text
	}
	// Keep head + tail so early goals and recent middle both survive.
	head := maxChars * 35 / 100
	tail := maxChars - head - 64
	if tail < 0 {
		tail = 0
	}
	return string(r[:head]) + "\n\n...[middle omitted for compact]...\n\n" + string(r[len(r)-tail:])
}

func buildGrokSummaryInputItem(field, summary string) ([]byte, error) {
	// Escape via json.Marshal for safety.
	type part struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if field == "messages" {
		item := map[string]any{
			"role":    "system",
			"content": "Conversation history compact summary (auto):\n" + summary,
		}
		return json.Marshal(item)
	}
	// Responses input item
	item := map[string]any{
		"type": "message",
		"role": "system",
		"content": []part{
			{Type: "input_text", Text: "Conversation history compact summary (auto):\n" + summary},
		},
	}
	return json.Marshal(item)
}

func (s *OpenAIGatewayService) callGrokSummaryCompact(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	model string,
	token string,
	proxyURL string,
	historyText string,
) (string, error) {
	if s == nil || s.httpUpstream == nil {
		return "", fmt.Errorf("upstream client unavailable")
	}
	summaryModel := firstNonEmpty(model, grokDefaultResponsesModel)
	// Prefer a stable chat model name for summary; mapping still applied by patch.
	prompt := "You are a context compaction engine. Summarize the conversation history below for continuing work.\n" +
		"Rules:\n" +
		"1) Preserve goals, constraints, decisions, file paths, bugs, APIs, and unfinished tasks.\n" +
		"2) Drop boilerplate, repeated tool dumps, and irrelevant chatter.\n" +
		"3) Be dense. Prefer bullet points. Max ~3000 words.\n" +
		"4) Output ONLY the summary, no preamble.\n\n" +
		"HISTORY:\n" + historyText

	reqBody := map[string]any{
		"model":             summaryModel,
		"stream":            false,
		"max_output_tokens": grokSummaryMaxOutputTokens,
		"input": []any{
			map[string]any{
				"type": "message",
				"role": "user",
				"content": []any{
					map[string]any{"type": "input_text", "text": prompt},
				},
			},
		},
		grokSummaryCompactMarker: true,
	}
	raw, err := json.Marshal(reqBody)
	if err != nil {
		return "", err
	}
	raw, err = patchGrokResponsesBody(raw, summaryModel)
	if err != nil {
		return "", err
	}

	callCtx, cancel := context.WithTimeout(ctx, grokSummaryTimeout)
	defer cancel()
	// Detach from client cancel so compact can finish even if client is impatient,
	// but still honor our timeout.
	callCtx = context.WithoutCancel(callCtx)

	upstreamReq, err := buildGrokResponsesRequest(callCtx, c, account, raw, token, "", s.cfg)
	if err != nil {
		return "", err
	}
	// Force non-stream accept for easier parse.
	upstreamReq.Header.Set("Accept", "application/json")

	resp, err := s.httpUpstream.Do(upstreamReq, proxyURL, account.ID, account.Concurrency)
	if err != nil {
		return "", err
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return "", err
	}
	if resp.StatusCode >= 400 {
		msg := sanitizeUpstreamErrorMessage(extractUpstreamErrorMessage(respBody))
		return "", fmt.Errorf("summary upstream %d: %s", resp.StatusCode, msg)
	}
	// Best-effort usage accounting from summary call.
	s.updateGrokUsageFromResponse(ctx, account, resp.Header, resp.StatusCode)

	text := extractGrokResponseOutputText(respBody)
	if strings.TrimSpace(text) == "" {
		return "", fmt.Errorf("summary response missing output text")
	}
	return text, nil
}

func extractGrokResponseOutputText(body []byte) string {
	// Common Responses shapes:
	// output[].content[].text / output_text
	var chunks []string
	output := gjson.GetBytes(body, "output")
	if output.IsArray() {
		output.ForEach(func(_, item gjson.Result) bool {
			content := item.Get("content")
			if content.IsArray() {
				content.ForEach(func(_, part gjson.Result) bool {
					t := strings.ToLower(part.Get("type").String())
					if t == "output_text" || t == "text" || t == "" {
						if s := strings.TrimSpace(part.Get("text").String()); s != "" {
							chunks = append(chunks, s)
						}
					}
					return true
				})
			}
			if s := strings.TrimSpace(item.Get("text").String()); s != "" {
				chunks = append(chunks, s)
			}
			return true
		})
	}
	if s := strings.TrimSpace(gjson.GetBytes(body, "output_text").String()); s != "" {
		chunks = append(chunks, s)
	}
	// chat.completions fallback
	if s := strings.TrimSpace(gjson.GetBytes(body, "choices.0.message.content").String()); s != "" {
		chunks = append(chunks, s)
	}
	return strings.TrimSpace(strings.Join(chunks, "\n"))
}
