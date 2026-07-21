package service

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestExtractGrokItemsPlainText(t *testing.T) {
	body := []byte(`{"input":[
		{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"world"}]}
	]}`)
	items := gjson.GetBytes(body, "input").Array()
	text := extractGrokItemsPlainText(items)
	require.Contains(t, text, "hello")
	require.Contains(t, text, "world")
	require.Contains(t, text, "[user]")
}

func TestBuildGrokSummaryInputItem_Responses(t *testing.T) {
	raw, err := buildGrokSummaryInputItem("input", "summary here")
	require.NoError(t, err)
	require.Contains(t, string(raw), "Conversation history compact summary")
	require.Contains(t, string(raw), "summary here")
	require.Equal(t, "system", gjson.GetBytes(raw, "role").String())
}

func TestBuildGrokSummaryInputItem_Messages(t *testing.T) {
	raw, err := buildGrokSummaryInputItem("messages", "ms")
	require.NoError(t, err)
	require.Equal(t, "system", gjson.GetBytes(raw, "role").String())
	require.Contains(t, gjson.GetBytes(raw, "content").String(), "ms")
}

func TestTrimGrokHistoryForSummary(t *testing.T) {
	s := strings.Repeat("a", 1000) + "MID" + strings.Repeat("b", 1000)
	out := trimGrokHistoryForSummary(s, 200)
	require.LessOrEqual(t, len([]rune(out)), 200+40) // marker adds a bit
	require.Contains(t, out, "omitted")
}

func TestGrokPromptArrayField_KeepSystem(t *testing.T) {
	body := []byte(`{"messages":[
		{"role":"system","content":"sys"},
		{"role":"user","content":"u1"},
		{"role":"assistant","content":"a1"},
		{"role":"user","content":"u2"}
	]}`)
	field, items, keepPrefix, err := grokPromptArrayField(body)
	require.NoError(t, err)
	require.Equal(t, "messages", field)
	require.Equal(t, 4, len(items))
	require.Equal(t, 1, keepPrefix)
}

func TestAutoCompactGrokPrompt_NoopSmall(t *testing.T) {
	s := &OpenAIGatewayService{}
	body := []byte(`{"model":"grok-4.5","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)
	out, meta, err := s.autoCompactGrokPrompt(nil, nil, &Account{ID: 1}, body, "grok-4.5", "", "")
	require.NoError(t, err)
	require.Equal(t, "noop", meta.Mode)
	require.Equal(t, string(body), string(out))
}

func TestAutoCompactGrokPrompt_HardDropFallbackWithoutToken(t *testing.T) {
	s := &OpenAIGatewayService{} // no httpUpstream / no token → hard drop path
	// Build body large enough that bytes/2 > budget (~492k)
	// budget=492000, need len(body) > 984000
	big := strings.Repeat("字", 120_000)
	items := make([]string, 0, 10)
	for i := 0; i < 10; i++ {
		items = append(items, `{"type":"message","role":"user","content":[{"type":"input_text","text":"`+big+`"}]}`)
	}
	body := []byte(`{"model":"grok-4.5","input":[` + strings.Join(items, ",") + `]}`)
	require.Greater(t, estimateGrokResponsesBodyTokens(body, "grok-4.5"), grokPromptTokenBudget("grok-4.5"))

	out, meta, err := s.autoCompactGrokPrompt(nil, nil, &Account{ID: 1, Platform: PlatformGrok}, body, "grok-4.5", "", "")
	require.NoError(t, err)
	require.Equal(t, "hard_drop", meta.Mode)
	require.Greater(t, meta.DroppedItems, 0)
	after := len(gjson.GetBytes(out, "input").Array())
	require.Less(t, after, 10)
	require.GreaterOrEqual(t, after, grokMinPromptInputItemsKeep)
}

func TestExtractGrokResponseOutputText(t *testing.T) {
	body := []byte(`{"output":[{"type":"message","content":[{"type":"output_text","text":"SUM"}]}]}`)
	require.Equal(t, "SUM", extractGrokResponseOutputText(body))
}
