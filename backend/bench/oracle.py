import json

from bench.cases import Case


class OracleProvider:
    """Replies with the case's reference fix, whatever it is asked.

    This is NOT a model. It measures the pipeline itself (apply, sandbox verification, regression
    checks) with a perfect proposer, so a run that is not 100% means the machinery is broken, not
    that a model was weak. It says nothing about how well any real model fixes bugs.
    """

    name = "oracle"
    model = "oracle (reference fix)"

    def __init__(self, case: Case):
        self._reply = json.dumps(
            {
                "explanation": f"Reference fix: {case.description}",
                "edits": [edit.model_dump() for edit in case.fix_edits],
            }
        )

    def available(self) -> tuple[bool, str]:
        return True, ""

    def generate(self, prompt, *, system=None, schema=None, max_tokens=700, temperature=None):
        return self._reply
