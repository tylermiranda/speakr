"""Default prompt templates — single source of truth.

Kept dependency-free (no app/model imports) so both ``src/init_db.py`` (which
seeds the admin setting on a fresh database) and ``src/tasks/processing.py``
(which uses it as a runtime fallback) can import it cheaply without circular
imports. Previously this text was copy-pasted in three places and could drift;
change it here only.
"""


# The summary prompt a fresh install ships with, and the fallback used at
# summarization time when no per-recording / tag / folder / user / admin prompt
# is set. To change the shipped default, edit this string.
DEFAULT_SUMMARY_PROMPT = """Identify the key issues discussed. First, give me minutes. Then, give me the key issues discussed. Then, any key takeaways. Then, any next steps (with responsible party for each step). Then, all important things that I didn't ask for but that need to be recorded. Make sure every important nuance is covered.

OUTPUT FORMAT (match this header structure exactly at the very top of your response):

# {Meeting Title or inferred short title}
**Date:** {meeting date}
**Time:** {meeting time}
**Participants:**
- {Participant 1}
- {Participant 2}

---

### Minutes

**1. {First Topic / Overview}:**
- {Details discussed...}

### Key Issues Discussed
1. {Issue 1...}

### Key Takeaways
- {Key takeaway 1...}

### Next Steps
| Action | Responsible Party |
|--------|-------------------|
| {Action item} | {Responsible person} |

### Additional Important Details
- {Important details...}

Rules:
- Always start the response with the Title, Date, Time, and Participants header.
- Use values from Context for Title, Date, Time, and Participants when available; otherwise infer from the transcript dialogue.
- If a value is unknown, write "unspecified" — never omit Title, Date, Time, or Participants.
- Make sure every important nuance, decision, and commitment is covered."""


# The admin-editable instruction body for CONTEXTUAL speaker labelling — used
# when a recording has no voice embeddings and the app falls back to naming
# speakers from the transcript, constrained to the user's saved speaker
# profiles. This is only the guidance paragraph; the code appends the concrete
# candidate list, the speaker labels, and the strict JSON output contract after
# it, so those cannot be broken by an edit here. It is deliberately the trailing
# (variable) part of a transcript-first prompt so it stays prefix-cache friendly
# (see PREFIX_CACHE_OPTIMIZED_PROMPTS): the transcript is the stable prefix, this
# guidance is the editable suffix.
DEFAULT_CONTEXTUAL_SPEAKER_PROMPT = """Work out which of the known speaker profiles each speaker label most likely corresponds to, using clues in the conversation such as the names people are addressed by, self-introductions, and references to roles or relationships. Only assign a known profile when the conversation clearly supports it; if you are not confident about a speaker, leave that speaker unassigned with an empty string. Never invent a name that is not in the known profiles list."""
