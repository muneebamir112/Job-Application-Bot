"""Word lists, regexes and small text helpers shared by the answer engine."""
import re

# ---------------------------------------------------------------- text helpers
def stem(w: str) -> str:
    w = w.lower()
    for suf in ("ings", "ing", "ied", "ies", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+(?:[./-][a-z0-9+#]+)*", (s or "").lower())


def wt(s: str) -> list[str]:
    """Word tokens; hyphen/slash words are split ('Python-based' -> python, based)."""
    return re.findall(r"[a-z0-9+#]+", (s or "").lower())


def wstems(s: str) -> set[str]:
    return {stem(t) for t in wt(s)}


def sentences(t: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", t or "") if s.strip()]


def word_clean(w: str) -> str:
    return w.strip(".,;:!?()\"'")


def norm_num(s: str) -> str:
    return re.sub(r"[,]", "", s).rstrip(".").lstrip("$")


def numbers(s: str) -> set[str]:
    return {norm_num(x) for x in re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", s or "")}


# ---------------------------------------------------------------- word lists
STOP = set("""a an the and or of to in on at for with from by as is are was were be been being do does did have has had it its this that these those i you your my me we
our us they their he she not no yes if then than so such can could would should will shall may might into over under between during before after up down out off again
here there when where why how all any both each few more most other some own same too very just also please one two""".split())

META = set("""tell describe share explain give provide time experience experiences role roles work worked working years year ever past current currently new example examples
using used use like well make made think include includes included describe about us us what which who whom whose looking look want hear heard""".split())

INTENT_GENERIC = set("""proud challeng strongest strong area technical comfort familiar best favorite main biggest greatest change impact impacted operate consider
project projects accomplish achievement overall general typical usual specific detail details""".split())

TECH = set("""python go golang rust c++ c# java javascript typescript ruby php swift kotlin scala perl sql nosql kubernetes docker terraform ansible jenkins aws azure gcp react
angular vue node django flask spring rails graphql kafka spark hadoop redis mongodb postgres postgresql mysql elasticsearch linux git grpc protobufs .net dotnet""".split())

RISKY = ["mentor", "manag", "management", "founder", "cofounder", "publish", "publication", "patent", "certif", "certification", "phd", "production", "bug", "incident",
         "oncall", "on-call", "promot", "award", "speaker", "clearance", "extensive", "expert", "proficient", "advanced", "seasoned"]
RISKY_STEMS = {stem(x) for x in RISKY}

# Intensifiers match the exact word only ('expertise' must not trip 'expert').
INTENSIFIERS = {"extensive", "expert", "proficient", "advanced", "seasoned"}

CLAIM_VERBS = {"led", "managed", "architected", "founded", "directed", "headed", "spearheaded", "launched", "drove",
               "owned", "oversaw", "supervised", "mentored", "coached"}

FEELINGS = {stem(w) for w in """passion passionate excited excite eager interest interested enjoy motivated believe value opportunity chance contribute align aligns fit fits
resonate resonates experience skills expertise background strong thrive aspire looking forward company role position team mission impact impactful meaningful work""".split()}

FLUFF = {stem(w) for w in """passionate passion excited excite eager thrive value believe interested opportunity contribute impact mission drive innovation innovative
motivated enjoy committed dedicated focus focused goal goals strong skilled solving solve complex challenges challenge growth learn""".split()}

LEDGER_TOPICS = [("authorization", r"authori[sz]|work status|right to work|legally"),
                 ("sponsorship", r"sponsor|visa"),
                 ("salary", r"salary|compensation|pay expectation|desired pay")]

# Demographic items are never copied into the 'form so far' memory.
DEMOGRAPHIC_RX = re.compile(r"gender|\brac(?:e|ial)\b|ethnic|veteran|disabilit|pronoun|\byour age\b|age range|how old|sexual|transgender|hispanic|latino", re.I)

# ---------------------------------------------------------------- regexes
URL_ASK = re.compile(r"\b(link|url|profile|website|web site|portfolio|address|handle|username)\b", re.I)
ASK_VERB = re.compile(r"\b(provide|share|enter|give|include|please|your|what is)\b", re.I)
EMPLOYED_BEFORE = re.compile(r"(previously|ever|formerly)\s+(worked|employed|been employed)|former employee|currently (an employee|employed by)", re.I)
URL_SITE = re.compile(r"github|linkedin|portfolio|website|web site|twitter|link|url|profile", re.I)

BEHAV = re.compile(r"(tell us about a time|describe a time|a time (when )?you|give (us )?an example of a time|how do you (handle|deal|approach|prioritize|manage|stay|keep|cope)|"
                   r"where do you see yourself|why are you (looking|leaving)|what motivates|weakness|\bstrengths?\b|disagree|conflict|failure|mistake|feedback|"
                   r"under pressure|tight deadline|work style|leadership style)", re.I)
COMPANY_RX = re.compile(r"why .*(work|join|interested|apply)|this (company|role|position)|why us|our (mission|company)|why do you want", re.I)

ID_IN_TEXT = re.compile(r"\[?\b[WSEMPJ]\d+(?:\.\d+)?\b\]?")
CLAIM_SENT = re.compile(r"\d|\bI\s+(?:\w+ed|led|built|ran|took|made|wrote|got|drove|won)\b", re.I)
PAST_EVENT = re.compile(r"\bI (led|built|developed|implemented|managed|worked|fixed|resolved|disagreed|presented|created|designed|optimi[sz]ed|launched|delivered|reduced|improved|mentored)\b", re.I)
META_REF = re.compile(r"previous question|my response to|the form|fact sheet|as stated above|as mentioned|\bfacts? to use\b", re.I)

# 'manag' must match managed/manager/management/managing, but NOT 'manageable'.
_MANAG_RX = re.compile(r"(?<![a-z])manag(?:e|ed|es|er|ers|ement|ing)(?![a-z])")


def hit(word: str, low: str) -> bool:
    """Word-beginning match (mentor -> mentorship). Intensifiers and 'manag' match whole words only; 'bug' also catches 'debug...'."""
    if word in INTENSIFIERS:
        return bool(re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low))
    if word == "manag":
        return bool(_MANAG_RX.search(low))
    return bool(re.search(r"(?<![a-z])" + re.escape(word), low)) or (word == "bug" and "debug" in low)
