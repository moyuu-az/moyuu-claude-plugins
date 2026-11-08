---
type: llm
---
PASS if the reply says where to put the new case (a new directory under myplug/evals/ holding a prompt.md and a graders/ directory) or recommends running `claude plugin eval init` to create it.
FAIL if the reply only reviews the existing cases without saying how to add one.
