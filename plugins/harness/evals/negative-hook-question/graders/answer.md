---
type: llm
---
PASS if the answer says that exit code 2 from a Stop hook prevents Claude from stopping (the turn or conversation continues) and that stderr is passed back to Claude as the reason.
FAIL if it says the hook only logs, that Claude stops anyway, or does not explain the effect.
