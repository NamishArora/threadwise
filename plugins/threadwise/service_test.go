package main

import (
	"strings"
	"testing"
	"time"
)

func TestAIQuestionRecognizesSupportedPrefixes(t *testing.T) {
	tests := []struct {
		message string
		want    string
		ok      bool
	}{
		{message: "@AI what did we promise?", want: "what did we promise?", ok: true},
		{message: "@threadwise when did we last discuss pricing?", want: "when did we last discuss pricing?", ok: true},
		{message: "ordinary message", ok: false},
	}
	for _, test := range tests {
		got, ok := aiQuestion(test.message)
		if got != test.want || ok != test.ok {
			t.Errorf("aiQuestion(%q) = (%q, %t), want (%q, %t)", test.message, got, ok, test.want, test.ok)
		}
	}
}

func TestStaleFollowupsRequireThresholdAndNoNewActivity(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	summaryAt := now.Add(-4 * 24 * time.Hour).Unix()
	metadata := map[string]any{
		"summary_at": float64(summaryAt),
		"open_followups": []any{
			map[string]any{"action": "Send the quote", "owner": "Alex"},
		},
	}
	if got := staleFollowups(metadata, now, summaryAt, 3); len(got) != 1 || !strings.Contains(got[0], "Send the quote") {
		t.Fatalf("expected overdue follow-up, got %v", got)
	}
	if got := staleFollowups(metadata, now, now.Unix(), 3); len(got) != 0 {
		t.Fatalf("new activity should suppress stale follow-ups, got %v", got)
	}
	if got := staleFollowups(metadata, now, summaryAt, 5); len(got) != 0 {
		t.Fatalf("follow-up below threshold should not be overdue, got %v", got)
	}
}

func TestTruncateContextKeepsRecentMessages(t *testing.T) {
	got := truncateContext("earlier recent newest", 12)
	if !strings.HasSuffix(got, "ecent newest") {
		t.Fatalf("truncated context should retain its most recent suffix, got %q", got)
	}
}

func TestScopedMemoriesKeepOnlySameUserThreadAndChannel(t *testing.T) {
	results := []memoryResult{
		{Memory: "same thread", UserID: "u1", Metadata: map[string]any{
			"application": "threadwise", "user_id": "u1", "memory_kind": "summary", "thread_id": "t1", "channel_id": "c1",
		}},
		{Memory: "other user's private detail", UserID: "u2", Metadata: map[string]any{
			"application": "threadwise", "user_id": "u2", "memory_kind": "summary", "thread_id": "t1", "channel_id": "c1",
		}},
		{Memory: "other thread", UserID: "u1", Metadata: map[string]any{
			"application": "threadwise", "user_id": "u1", "memory_kind": "summary", "thread_id": "t2", "channel_id": "c1",
		}},
		{Memory: "same user's preference", UserID: "u1", Metadata: map[string]any{
			"application": "threadwise", "user_id": "u1", "memory_kind": "preference",
		}},
	}

	got := scopedMemories(results, "u1", "t1", "c1")
	if len(got) != 2 || got[0] != "same thread" || got[1] != "same user's preference" {
		t.Fatalf("unexpected memory scope results: %v", got)
	}
}
