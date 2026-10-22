from dataclasses import dataclass, field


@dataclass
class AnswerResult:
    text: str                                    # always non-empty
    color: str = "YELLOW"                        # "GREEN" | "YELLOW"
    via: str = "model"                           # model | salvage | restate | summary | rule | no-data
    route: str = "FACTUAL"                       # LEDGER | URLID | COMPANY | BEHAVIORAL | FACTUAL | CHOICE | NUMERIC
    missing_words: list = field(default_factory=list)
    cited_ids: list = field(default_factory=list)
    attempts: list = field(default_factory=list)     # per attempt: answer, cited ids, rejection reasons
    calls: list = field(default_factory=list)        # per model call metrics
    elapsed_s: float = 0.0
    question: str = ""

    def tag(self) -> str:
        return f"[v2] {self.color} route={self.route} via={self.via} cites={','.join(self.cited_ids) or '-'}"
