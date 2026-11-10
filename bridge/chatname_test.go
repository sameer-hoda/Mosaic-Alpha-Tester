package main

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

// Fixture mirrors the polluted live names: bare LIDs, the me-LID, and "."
// contact names must never be persisted as chat labels.
func TestCleanChatName(t *testing.T) {
	cases := []struct {
		name    string
		jid     types.JID
		chatJID string
		own     string
		want    string
	}{
		{"111111111111111", types.NewJID("111111111111111", "lid"), "111111111111111@lid", "", ""},
		{"", types.NewJID("222222222222222", "lid"), "222222222222222@lid", "", ""},
		{".", types.NewJID("222222222222222", "lid"), "222222222222222@lid", "", ""},
		{"*", types.NewJID("222222222222222", "lid"), "222222222222222@lid", "", ""},
		{"Sam Rao", types.NewJID("15551234567", "s.whatsapp.net"), "15551234567@s.whatsapp.net", "Sam Rao", ""},
		{"Raghav", types.NewJID("333333333333333", "lid"), "333333333333333@lid", "", "Raghav"},
		{"Naina", types.NewJID("444444444444444", "lid"), "444444444444444@lid", "", "Naina"},
		{"Project Atlas", types.NewJID("120363000000000001", "g.us"), "120363000000000001@g.us", "", "Project Atlas"},
	}
	for _, tc := range cases {
		if got := cleanChatName(tc.name, tc.jid, tc.chatJID, tc.own); got != tc.want {
			t.Fatalf("cleanChatName(%q) = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestDMLabel(t *testing.T) {
	cases := map[string]string{
		"222222222222222@lid":        "DM 2222",
		"15551234567@s.whatsapp.net": "DM 4567",
		"1234@lid":                   "DM 1234",
		"@lid":                       "DM",
	}
	for jid, want := range cases {
		if got := dmLabel(jid); got != want {
			t.Fatalf("dmLabel(%q) = %q, want %q", jid, got, want)
		}
	}
}

func TestBareNumericAndPunctuation(t *testing.T) {
	if !bareNumeric("111111111111111") {
		t.Fatal("bare LID must be numeric")
	}
	if bareNumeric("Raghav") {
		t.Fatal("a name must not be numeric")
	}
	if !punctuationOnly(".*") || !punctuationOnly("—") {
		t.Fatal("punctuation-only names must be detected")
	}
	if punctuationOnly("DM 1100") {
		t.Fatal("a real label is not punctuation-only")
	}
}
