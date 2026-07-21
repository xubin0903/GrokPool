package service

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func TestCompactGrokResponsesInputIfNeeded_DropsOldestInput(t *testing.T) {
	// Build a huge input array that clearly exceeds budget by byte estimate.
	items := make([]string, 0, 20)
	big := strings.Repeat("字", 80_000) // dense CJK text
	for i := 0; i < 12; i++ {
		items = append(items, `{"type":"message","role":"user","content":[{"type":"input_text","text":"`+big+`"}]}`)
	}
	body := []byte(`{"model":"grok-4.5","input":[` + strings.Join(items, ",") + `]}`)
	before := len(gjson.GetBytes(body, "input").Array())
	require.Greater(t, before, grokMinPromptInputItemsKeep)

	out, dropped, err := compactGrokResponsesInputIfNeeded(body, "grok-4.5")
	require.NoError(t, err)
	require.Greater(t, dropped, 0)
	after := len(gjson.GetBytes(out, "input").Array())
	require.Less(t, after, before)
	require.GreaterOrEqual(t, after, grokMinPromptInputItemsKeep)
	// Still valid JSON model field
	require.Equal(t, "grok-4.5", gjson.GetBytes(out, "model").String())
}

func TestCompactGrokResponsesInputIfNeeded_PreservesSystemMessages(t *testing.T) {
	big := strings.Repeat("x", 120_000)
	body := []byte(`{
	  "model":"grok-4.5",
	  "messages":[
	    {"role":"system","content":"you are helpful"},
	    {"role":"user","content":"` + big + `"},
	    {"role":"assistant","content":"` + big + `"},
	    {"role":"user","content":"` + big + `"},
	    {"role":"user","content":"final"}
	  ]
	}`)
	out, dropped, err := compactGrokResponsesInputIfNeeded(body, "grok-4.5")
	require.NoError(t, err)
	// Either compacted or already under budget depending on estimate; if compacted, system stays first.
	if dropped > 0 {
		msgs := gjson.GetBytes(out, "messages").Array()
		require.NotEmpty(t, msgs)
		require.Equal(t, "system", msgs[0].Get("role").String())
	}
}

func TestCompactGrokResponsesInputIfNeeded_SmallBodyNoop(t *testing.T) {
	body := []byte(`{"model":"grok-4.5","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)
	out, dropped, err := compactGrokResponsesInputIfNeeded(body, "grok-4.5")
	require.NoError(t, err)
	require.Equal(t, 0, dropped)
	require.Equal(t, string(body), string(out))
}

func TestGrokPromptTokenBudget(t *testing.T) {
	b := grokPromptTokenBudget("grok-4.5")
	require.Equal(t, grokDefaultMaxPromptTokens-grokPromptTokenSafetyMargin, b)
}

// ensure sjson still works for our rewrite path
func TestCompactSetRawInput(t *testing.T) {
	body := []byte(`{"input":[{"a":1},{"a":2},{"a":3}]}`)
	next, err := sjson.SetRawBytes(body, "input", []byte(`[{"a":2},{"a":3}]`))
	require.NoError(t, err)
	require.Equal(t, 2, len(gjson.GetBytes(next, "input").Array()))
}
