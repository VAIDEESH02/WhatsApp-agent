"""Constants shared across the WhatsApp agent."""

# A message consisting only of one of these (ignoring case and trailing punctuation)
# carries no reply style worth learning from.
ONE_WORD_ACKS = {
    "ok",
    "okay",
    "k",
    "kk",
    "haan",
    "hmm",
    "thanks",
    "thank you",
    "cool",
    "nice",
}
