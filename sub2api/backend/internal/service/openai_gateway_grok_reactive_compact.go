package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const (
	// xAI grok-4.5 single-request prompt hard cap is ~500k tokens.
	grokDefaultMaxPromptTokens  = 500_000
	grokPromptTokenSafetyMargin = 8_000
	grokMinPromptInputItemsKeep = 2

	// Side-channel summary budget (must stay under the 500k hard cap).
	grokSummarySourceTokenBudget = 380_000
	grokSummaryKeepRecentItems   = 4
	grokSummaryMaxOutputTokens   = 4096
	grokSummarySourceMaxChars    = grokSummarySourceTokenBudget * 2
	grokSummaryTimeout           = 75 * time.Second
	// Nested summary calls must never re-enter compact.
	grokSummaryCompactMarker = "__sub2api_grok_summary_compact__"
)

type grokPromptCompactMeta struct {
	Mode            string // noop | summary | hard_drop | summary+hard_drop
	DroppedItems    int
	Summarized      bool
	SummaryChars    int
	SummaryAccount  int64
	BeforeEstTokens int
	AfterEstTokens  int
	BeforeBytes     int
	AfterBytes      int
}

// isGrokPromptTooLargeResponse reports whether an upstream 400 is a single-request
// prompt/context overflow (NOT free-usage exhausted / encrypted_content / generic 400).
func isGrokPromptTooLargeResponse(statusCode int, body []byte) bool {
	if statusCode != http.StatusBadRequest {
		return false
	}
	// Free-usage exhaustion is a quota problem, not compact material.
	if isGrokFreeUsageExhaustedBody(body) {
		return false
	}
	// Invalid encrypted reasoning has its own retry path.
	if isGrokInvalidEncryptedContentResponse(statusCode, body) {
		return false
	}

	upstreamMsg := sanitizeUpstreamErrorMessage(extractUpstreamErrorMessage(body))
	if isOpenAIContextWindowError(upstreamMsg, body) {
		return true
	}
	return isGrokPromptTooLargeMessage(upstreamMsg, body)
}

func isGrokPromptTooLargeMessage(upstreamMsg string, body []byte) bool {
	match := func(text string) bool {
		lower := strings.ToLower(strings.TrimSpace(text))
		if lower == "" {
			return false
		}
		// Prefer high-signal phrases (whitelist — never over-match).
		needles := []string{
			"prompt is too long",
			"prompt too long",
			"prompt is too large",
			"input is too long",
			"input too long",
			"input is too large",
			"request too large",
			"payload too large",
			"maximum prompt",
			"max prompt",
			"prompt length",
			"input length",
			"prompt tokens",
			"input tokens",
			"token limit",
			"tokens exceed",
			"exceeds the maximum",
			"exceeded the maximum",
			"exceeds maximum",
			"context window",
			"context length",
			"context_too_large",
			"context_length_exceeded",
		}
		for _, n := range needles {
			if strings.Contains(lower, n) {
				// Avoid matching free-usage / daily quota phrasing.
				if strings.Contains(lower, "free usage") ||
					strings.Contains(lower, "daily") ||
					strings.Contains(lower, "24h") ||
					strings.Contains(lower, "rate limit") {
					continue
				}
				return true
			}
		}
		// "500000" / "500,000" / "500k" near token/prompt language.
		if (strings.Contains(lower, "500000") || strings.Contains(lower, "500,000") || strings.Contains(lower, "500k")) &&
			(strings.Contains(lower, "token") || strings.Contains(lower, "prompt") || strings.Contains(lower, "input")) {
			return true
		}
		return false
	}
	if match(upstreamMsg) {
		return true
	}
	for _, path := range []string{
		"error.message",
		"error",
		"message",
		"error.code",
		"code",
	} {
		if match(gjson.GetBytes(body, path).String()) {
			return true
		}
	}
	return match(string(body))
}

// compactGrokPromptAfterOversize is the reactive path:
// 1) try summary with the fattest remaining-quota Grok account
// 2) fall back to hard-drop on the (possibly summarized) body
// Returns compacted body + meta. err only for hard failures that should 502
// without retry (e.g. nothing droppable and summary failed).
func (s *OpenAIGatewayService) compactGrokPromptAfterOversize(
	ctx context.Context,
	c *gin.Context,
	primary *Account,
	body []byte,
	model string,
) ([]byte, grokPromptCompactMeta, error) {
	meta := grokPromptCompactMeta{
		Mode:        "noop",
		BeforeBytes: len(body),
	}
	if len(body) == 0 {
		return body, meta, fmt.Errorf("empty body cannot compact")
	}
	if gjson.GetBytes(body, grokSummaryCompactMarker).Bool() {
		return body, meta, fmt.Errorf("nested summary compact marker set")
	}

	budget := grokPromptTokenBudget(model)
	before := estimateGrokResponsesBodyTokens(body, model)
	meta.BeforeEstTokens = before

	out := body
	// L1: summary via max-remaining-quota account.
	if s != nil && s.httpUpstream != nil {
		summaryAccount := s.pickGrokMaxRemainingQuotaAccount(ctx, primary)
		if summaryAccount != nil {
			token, _, terr := s.getRequestCredential(ctx, c, summaryAccount)
			if terr == nil && strings.TrimSpace(token) != "" {
				proxyURL := ""
				if summaryAccount.ProxyID != nil && summaryAccount.Proxy != nil {
					proxyURL = summaryAccount.Proxy.URL()
				}
				sumOut, sumMeta, sumErr := s.summaryCompactGrokPrompt(ctx, c, summaryAccount, body, model, token, proxyURL, budget)
				if sumErr == nil {
					out = sumOut
					meta = sumMeta
					meta.BeforeEstTokens = before
					meta.BeforeBytes = len(body)
					meta.SummaryAccount = summaryAccount.ID
					meta.AfterEstTokens = estimateGrokResponsesBodyTokens(out, model)
					meta.AfterBytes = len(out)
					if meta.AfterEstTokens <= budget {
						return out, meta, nil
					}
				} else {
					slog.Warn("grok_prompt_summary_compact_failed",
						"primary_account_id", accountIDOrZero(primary),
						"summary_account_id", summaryAccount.ID,
						"model", model,
						"error", sumErr.Error(),
					)
				}
			} else if terr != nil {
				slog.Warn("grok_prompt_summary_credential_failed",
					"summary_account_id", summaryAccount.ID,
					"error", terr.Error(),
				)
			}
		}
	}

	// L2: hard-drop oldest droppable items.
	hard, dropped, herr := compactGrokResponsesInputIfNeeded(out, model)
	if herr != nil {
		return body, meta, herr
	}
	if dropped > 0 {
		if meta.Mode == "summary" || meta.Summarized {
			meta.Mode = "summary+hard_drop"
		} else {
			meta.Mode = "hard_drop"
		}
		meta.DroppedItems = dropped
		out = hard
		meta.AfterEstTokens = estimateGrokResponsesBodyTokens(out, model)
		meta.AfterBytes = len(out)
		return out, meta, nil
	}

	// Nothing changed → cannot salvage.
	if meta.Mode == "noop" || !meta.Summarized {
		meta.AfterEstTokens = before
		meta.AfterBytes = len(body)
		return body, meta, fmt.Errorf("compact produced no reduction")
	}
	// Summary ran but still over and hard-drop could not help — still try the summary body.
	meta.AfterEstTokens = estimateGrokResponsesBodyTokens(out, model)
	meta.AfterBytes = len(out)
	return out, meta, nil
}

func accountIDOrZero(a *Account) int64 {
	if a == nil {
		return 0
	}
	return a.ID
}

// pickGrokMaxRemainingQuotaAccount chooses a schedulable Grok account with the
// highest observed remaining token quota. Falls back to primary.
func (s *OpenAIGatewayService) pickGrokMaxRemainingQuotaAccount(ctx context.Context, primary *Account) *Account {
	if primary != nil && primary.IsGrok() {
		// Always a valid fallback.
	}
	if s == nil || s.accountRepo == nil {
		return primary
	}
	if ctx == nil {
		ctx = context.Background()
	}
	accounts, err := s.accountRepo.ListSchedulableByPlatform(ctx, PlatformGrok)
	if err != nil || len(accounts) == 0 {
		return primary
	}

	var best *Account
	var bestRemaining int64 = -1
	var bestKnown bool

	consider := func(a *Account) {
		if a == nil || !a.IsGrok() || !a.IsSchedulable() {
			return
		}
		// Skip accounts currently rate-limited / temp-unschedulable (IsSchedulable covers this).
		rem, known := grokAccountRemainingTokens(a)
		if known {
			if !bestKnown || rem > bestRemaining {
				cp := *a
				best = &cp
				bestRemaining = rem
				bestKnown = true
			}
			return
		}
		// Unknown remaining: keep as weak candidate only if nothing better.
		if best == nil {
			cp := *a
			best = &cp
		}
	}

	for i := range accounts {
		consider(&accounts[i])
	}
	if best == nil {
		return primary
	}
	// Prefer primary when it ties or is the only unknown.
	if primary != nil && best.ID == primary.ID {
		return primary
	}
	if primary != nil {
		prem, pknown := grokAccountRemainingTokens(primary)
		if pknown && bestKnown && prem >= bestRemaining {
			return primary
		}
	}
	// Reload full account (with credentials) by ID when we only have list projection.
	if s.accountRepo != nil && best.ID > 0 {
		if full, gerr := s.accountRepo.GetByID(ctx, best.ID); gerr == nil && full != nil {
			return full
		}
	}
	return best
}

func grokAccountRemainingTokens(a *Account) (remaining int64, known bool) {
	if a == nil {
		return 0, false
	}
	snapshot, err := grokQuotaSnapshotFromExtra(a.Extra)
	if err != nil || snapshot == nil || snapshot.Tokens == nil || snapshot.Tokens.Remaining == nil {
		return 0, false
	}
	return *snapshot.Tokens.Remaining, true
}

// compactGrokResponsesInputIfNeeded drops oldest Responses "input" (or chat
// "messages") items when estimated prompt exceeds Grok's ~500k budget.
// Fast byte-length estimates only — never tiktoken on multi-MB bodies.
func compactGrokResponsesInputIfNeeded(body []byte, model string) ([]byte, int, error) {
	if len(body) == 0 {
		return body, 0, nil
	}
	budget := grokPromptTokenBudget(model)
	if estimateGrokResponsesBodyTokens(body, model) <= budget {
		return body, 0, nil
	}
	if input := gjson.GetBytes(body, "input"); input.Exists() && input.IsArray() {
		return compactGrokJSONArrayField(body, "input", model, budget)
	}
	if messages := gjson.GetBytes(body, "messages"); messages.Exists() && messages.IsArray() {
		return compactGrokJSONArrayField(body, "messages", model, budget)
	}
	return body, 0, nil
}

func compactGrokJSONArrayField(body []byte, field string, model string, budget int) ([]byte, int, error) {
	items := gjson.GetBytes(body, field).Array()
	n := len(items)
	if n <= grokMinPromptInputItemsKeep {
		return body, 0, nil
	}

	// Preserve leading system/developer messages.
	keepPrefix := 0
	for keepPrefix < n {
		role := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("role").String()))
		typ := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("type").String()))
		if field == "input" && typ != "" && typ != "message" {
			break
		}
		if role != "system" && role != "developer" {
			break
		}
		keepPrefix++
	}

	arrayRawLen := len(gjson.GetBytes(body, field).Raw)
	overheadBytes := len(body) - arrayRawLen
	if overheadBytes < 0 {
		overheadBytes = 0
	}

	sizes := make([]int, n)
	for i, it := range items {
		sizes[i] = len(it.Raw)
	}

	// Drop from the oldest droppable item until estimated tokens fit.
	start := keepPrefix
	minStart := n - grokMinPromptInputItemsKeep
	if minStart < keepPrefix {
		minStart = keepPrefix
	}
	for start < minStart {
		sum := overheadBytes
		for i := 0; i < keepPrefix; i++ {
			sum += sizes[i]
		}
		for i := start; i < n; i++ {
			sum += sizes[i]
		}
		keptCount := keepPrefix + (n - start)
		if keptCount > 1 {
			sum += keptCount - 1
		}
		if sum/2 <= budget {
			break
		}
		remainingDroppable := minStart - start
		step := remainingDroppable / 2
		if step < 1 {
			step = 1
		}
		start += step
		if start > minStart {
			start = minStart
		}
	}

	dropped := start - keepPrefix
	if dropped <= 0 {
		return body, 0, nil
	}

	kept := make([]gjson.Result, 0, keepPrefix+(n-start))
	kept = append(kept, items[:keepPrefix]...)
	kept = append(kept, items[start:]...)
	rawParts := make([][]byte, 0, len(kept))
	for _, it := range kept {
		rawParts = append(rawParts, []byte(it.Raw))
	}
	joined := append(append([]byte{'['}, bytes.Join(rawParts, []byte{','})...), ']')
	next, err := sjson.SetRawBytes(body, field, joined)
	if err != nil {
		return body, 0, err
	}
	return next, dropped, nil
}

func grokPromptTokenBudget(model string) int {
	_ = model
	budget := grokDefaultMaxPromptTokens - grokPromptTokenSafetyMargin
	if budget < 50_000 {
		return 50_000
	}
	return budget
}

// estimateGrokResponsesBodyTokens is a FAST overestimate (no tiktoken).
func estimateGrokResponsesBodyTokens(body []byte, model string) int {
	_ = model
	if len(body) == 0 {
		return 0
	}
	return len(body) / 2
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

	recentStart := n - grokSummaryKeepRecentItems
	if recentStart < keepPrefix {
		recentStart = keepPrefix
	}
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
	for keepPrefix < len(items) {
		role := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("role").String()))
		typ := strings.ToLower(strings.TrimSpace(items[keepPrefix].Get("type").String()))
		if field == "input" && typ != "" && typ != "message" {
			break
		}
		if role != "system" && role != "developer" {
			break
		}
		keepPrefix++
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
				// Skip image payloads.
			default:
				if s := strings.TrimSpace(part.Get("text").String()); s != "" {
					parts = append(parts, s)
				}
			}
			return true
		})
		return strings.Join(parts, "\n")
	}
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
	head := maxChars * 35 / 100
	tail := maxChars - head - 64
	if tail < 0 {
		tail = 0
	}
	return string(r[:head]) + "\n\n...[middle omitted for compact]...\n\n" + string(r[len(r)-tail:])
}

func buildGrokSummaryInputItem(field, summary string) ([]byte, error) {
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
	callCtx = context.WithoutCancel(callCtx)

	upstreamReq, err := buildGrokResponsesRequest(callCtx, c, account, raw, token, "", s.cfg)
	if err != nil {
		return "", err
	}
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
	s.updateGrokUsageFromResponse(ctx, account, resp.Header, resp.StatusCode)

	text := extractGrokResponseOutputText(respBody)
	if strings.TrimSpace(text) == "" {
		return "", fmt.Errorf("summary response missing output text")
	}
	return text, nil
}

func extractGrokResponseOutputText(body []byte) string {
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
	if s := strings.TrimSpace(gjson.GetBytes(body, "choices.0.message.content").String()); s != "" {
		chunks = append(chunks, s)
	}
	return strings.TrimSpace(strings.Join(chunks, "\n"))
}

// writeGrokPromptCompactFailed502 returns a hard 502 after compact could not salvage
// an oversize prompt (or the post-compact retry still failed).
func writeGrokPromptCompactFailed502(c *gin.Context, detail string) error {
	msg := "Grok prompt exceeds single-request limit after compact"
	if d := strings.TrimSpace(detail); d != "" {
		msg = msg + ": " + d
	}
	if c != nil {
		MarkResponseCommitted(c)
		c.JSON(http.StatusBadGateway, gin.H{
			"error": gin.H{
				"type":    "api_error",
				"message": msg,
			},
		})
	}
	return fmt.Errorf("grok prompt compact failed: %s", msg)
}
