package service

import (
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestParseGrokActualLimitTokens(t *testing.T) {
	a, b, ok := parseGrokActualLimitTokens(`tokens (actual/limit): 1124730/1000000`)
	require.True(t, ok)
	require.EqualValues(t, 1124730, a)
	require.EqualValues(t, 1000000, b)
}

func TestParseGrokFreeUsageExhaustedBody(t *testing.T) {
	body := []byte(`{"code":"subscription:free-usage-exhausted","error":"You've used all the included free usage for model grok-4.5-build-free for now. Usage resets over a rolling 24-hour window — tokens (actual/limit): 1124730/1000000."}`)
	snap := parseGrokFreeUsageExhaustedBody(body, time.Now())
	require.NotNil(t, snap)
	require.NotNil(t, snap.Tokens)
	require.NotNil(t, snap.Tokens.Remaining)
	require.EqualValues(t, 0, *snap.Tokens.Remaining)
	require.NotNil(t, snap.Tokens.Limit)
	require.EqualValues(t, 1000000, *snap.Tokens.Limit)
}

func TestIsGrokFreeUsageExhaustedBody(t *testing.T) {
	require.True(t, isGrokFreeUsageExhaustedBody([]byte(`subscription:free-usage-exhausted`)))
	require.False(t, isGrokFreeUsageExhaustedBody([]byte(`permission-denied`)))
}
