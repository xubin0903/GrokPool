package service

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestIsGrokPromptTooLargeResponse(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name   string
		status int
		body   string
		want   bool
	}{
		{
			name:   "context_length_exceeded",
			status: 400,
			body:   `{"error":{"message":"context_length_exceeded: maximum context length exceeded","type":"invalid_request_error"}}`,
			want:   true,
		},
		{
			name:   "prompt too long plain",
			status: 400,
			body:   `{"error":"prompt is too long: 612345 tokens > 500000"}`,
			want:   true,
		},
		{
			name:   "500k token phrasing",
			status: 400,
			body:   `{"message":"Input tokens 520000 exceed the 500000 limit"}`,
			want:   true,
		},
		{
			name:   "not 400",
			status: 429,
			body:   `{"error":"prompt is too long"}`,
			want:   false,
		},
		{
			name:   "generic 400",
			status: 400,
			body:   `{"error":"invalid tool schema"}`,
			want:   false,
		},
		{
			name:   "free usage exhausted not compact",
			status: 400,
			body:   `{"code":"SomeCode","error":"Free usage limit of 1000000 tokens has been exhausted. Actual: 1000001 Limit: 1000000"}`,
			want:   false,
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := isGrokPromptTooLargeResponse(tc.status, []byte(tc.body))
			if got != tc.want {
				t.Fatalf("isGrokPromptTooLargeResponse() = %v, want %v (body=%s)", got, tc.want, tc.body)
			}
		})
	}
}

func TestCompactGrokResponsesInputIfNeeded_DropsOldest(t *testing.T) {
	t.Parallel()

	// Build a body whose byte estimate is well over budget so hard-drop must fire.
	// estimate = len(body)/2; budget ≈ 492000 → need body > ~984000 bytes.
	// Use many large user messages so dropping oldest reduces size.
	const itemCount = 40
	const itemPad = 60_000 // ~60KB text each → ~2.4MB total
	items := make([]map[string]any, 0, itemCount+1)
	items = append(items, map[string]any{
		"type": "message",
		"role": "system",
		"content": []map[string]string{
			{"type": "input_text", "text": "SYSTEM_KEEP"},
		},
	})
	pad := strings.Repeat("x", itemPad)
	for i := 0; i < itemCount; i++ {
		items = append(items, map[string]any{
			"type": "message",
			"role": "user",
			"content": []map[string]string{
				{"type": "input_text", "text": pad + "-item-" + itoa(i)},
			},
		})
	}
	bodyMap := map[string]any{
		"model": "grok-4.5",
		"input": items,
	}
	body, err := json.Marshal(bodyMap)
	if err != nil {
		t.Fatal(err)
	}
	if estimateGrokResponsesBodyTokens(body, "grok-4.5") <= grokPromptTokenBudget("grok-4.5") {
		t.Fatalf("test setup too small: est=%d budget=%d len=%d",
			estimateGrokResponsesBodyTokens(body, "grok-4.5"),
			grokPromptTokenBudget("grok-4.5"),
			len(body),
		)
	}

	out, dropped, err := compactGrokResponsesInputIfNeeded(body, "grok-4.5")
	if err != nil {
		t.Fatalf("compact error: %v", err)
	}
	if dropped <= 0 {
		t.Fatalf("expected dropped items > 0, got %d", dropped)
	}
	if estimateGrokResponsesBodyTokens(out, "grok-4.5") > grokPromptTokenBudget("grok-4.5") {
		// May still be over if residual is huge; at least must be smaller.
		if len(out) >= len(body) {
			t.Fatalf("compact did not shrink body: before=%d after=%d dropped=%d", len(body), len(out), dropped)
		}
	}
	// System prefix must survive.
	if !strings.Contains(string(out), "SYSTEM_KEEP") {
		t.Fatal("system prefix was dropped")
	}
	// Newest item should survive.
	if !strings.Contains(string(out), "-item-"+itoa(itemCount-1)) {
		t.Fatal("newest item was dropped")
	}
}

func TestCompactGrokResponsesInputIfNeeded_UnderBudgetNoop(t *testing.T) {
	t.Parallel()
	body := []byte(`{"model":"grok-4.5","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}`)
	out, dropped, err := compactGrokResponsesInputIfNeeded(body, "grok-4.5")
	if err != nil {
		t.Fatal(err)
	}
	if dropped != 0 {
		t.Fatalf("expected noop drop=0, got %d", dropped)
	}
	if string(out) != string(body) {
		t.Fatal("under-budget body was mutated")
	}
}

func TestGrokAccountRemainingTokens(t *testing.T) {
	t.Parallel()
	rem := int64(777_000)
	a := &Account{
		Extra: map[string]any{
			grokQuotaSnapshotExtraKey: map[string]any{
				"tokens": map[string]any{
					"remaining": rem,
					"limit":     int64(1_000_000),
				},
			},
		},
	}
	got, known := grokAccountRemainingTokens(a)
	if !known || got != rem {
		t.Fatalf("remaining=%d known=%v want %d true", got, known, rem)
	}
	if _, known := grokAccountRemainingTokens(&Account{}); known {
		t.Fatal("empty account should be unknown")
	}
}

func TestIsGrokPromptTooLargeIgnoresEncrypted(t *testing.T) {
	t.Parallel()
	// encrypted content body shape used by isGrokInvalidEncryptedContentResponse
	body := []byte(`{"code":"Client specified an invalid argument","error":"Incorrect padding for encrypted content"}`)
	// Even if message mentions tokens, encrypted path should win if detector matches.
	// Our oversize detector explicitly excludes encrypted responses.
	if isGrokInvalidEncryptedContentResponse(http.StatusBadRequest, body) {
		if isGrokPromptTooLargeResponse(http.StatusBadRequest, body) {
			t.Fatal("encrypted content must not be treated as oversize")
		}
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [20]byte
	pos := len(b)
	n := i
	if n < 0 {
		n = -n
	}
	for n > 0 {
		pos--
		b[pos] = byte('0' + n%10)
		n /= 10
	}
	if i < 0 {
		pos--
		b[pos] = '-'
	}
	return string(b[pos:])
}
