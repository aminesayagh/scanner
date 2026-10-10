"""Validation: do a family's sampled pages share one template?

Usable samples (fetched, not an error/challenge page, not redirected elsewhere) are clustered by the
Jaccard overlap of their main-content structure; samples whose structured data names different things
(Product vs Article) never share a cluster. Then:
- one cluster of at least `min_agreeing_samples`: the family is validated on its sample, and samples outside
  it become literal exceptions (/product/compare);
- several such clusters: one route per template (same pattern), and unsampled members are ambiguous since
  their URL cannot tell which template they use;
- none, with enough usable samples: unresolved, observed pages kept individually;
- too few usable samples: still provisional.
A redirected or failed sample is not evidence against its family: it is set aside, never collapses it.
"""

from dataclasses import dataclass, field

from .features import ROLE_TYPES
from .models import Config, PageObservation, RouteCandidate, URLRecord
from .normalization import normalize


def unusable_reason(obs: PageObservation, record: URLRecord) -> str | None:
    if obs.fetch_result != "ok":
        return obs.fetch_result
    if obs.final_url and normalize(obs.final_url)["crawl_url"] != record.crawl_url:
        return "redirected"
    return None


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def clusters(samples: list[tuple[str, set, frozenset]], threshold: float) -> list[list[str]]:
    """Samples (id, structure, role types) in groups, largest first. Each joins the group it overlaps most
    on average, if that reaches `threshold` and their role types match; ids in sorted order keep it stable."""
    groups: list[tuple[frozenset, list]] = []
    for uid, feats, roles in sorted(samples, key=lambda s: s[0]):
        best, best_score = None, threshold
        for g in groups:
            if g[0] != roles:
                continue
            score = sum(jaccard(feats, f) for _, f in g[1]) / len(g[1])
            if score >= best_score:
                best, best_score = g, score
        if best is None:
            groups.append((roles, [(uid, feats)]))
        else:
            best[1].append((uid, feats))
    ordered = sorted(([u for u, _ in g[1]] for g in groups), key=lambda ids: (-len(ids), ids[0]))
    return ordered


@dataclass
class Outcome:
    kind: str  # "validated" | "provisional" | "unresolved"
    templates: list[list[str]] = field(default_factory=list)  # agreeing groups, largest first
    exceptions: list[str] = field(default_factory=list)  # usable samples outside every template
    loose: list[str] = field(default_factory=list)  # usable samples of an unresolved or provisional family
    unusable: dict[str, str] = field(default_factory=dict)
    wants_more: bool = False


def judge(members: list[str], observations: dict[str, PageObservation], records: dict[str, URLRecord],
          config: Config) -> Outcome:
    sampled = [u for u in members if u in observations]
    unusable = {u: r for u in sampled if (r := unusable_reason(observations[u], records[u]))}
    usable = [u for u in sampled if u not in unusable]
    groups = clusters([(u, set(observations[u].features), frozenset(set(observations[u].structured_data_types) & ROLE_TYPES))
                       for u in usable], config.similarity_threshold)
    templates = [g for g in groups if len(g) >= config.min_agreeing_samples]
    rest = sorted(u for g in groups if len(g) < config.min_agreeing_samples for u in g)
    more = len(sampled) < len(members)
    if templates:
        return Outcome("validated", templates, rest, [], unusable, wants_more=bool(rest) and more)
    kind = "unresolved" if len(usable) >= config.min_agreeing_samples else "provisional"
    return Outcome(kind, [], [], sorted(usable), unusable, wants_more=more)


def round_robin(queues: list[list[str]], budget: int) -> list[str]:
    picked, queues = [], [list(q) for q in queues]
    while len(picked) < budget and any(queues):
        for q in queues:
            if q and len(picked) < budget:
                picked.append(q.pop(0))
    return picked


def plan_samples(candidates: list[RouteCandidate], observed: set[str], budget: int, per_family: int) -> list[str]:
    """First-round samples, one per family in turn (largest families first) so no family takes the whole
    budget. Pages already fetched during discovery count as samples and cost nothing."""
    queues = []
    for c in sorted(candidates, key=lambda c: (-len(c.member_ids), c.pattern)):
        have = len(observed & set(c.member_ids))
        queues.append([u for u in c.sample_ids if u not in observed][: max(0, per_family - have)])
    return round_robin(queues, budget)


def plan_extra(candidates: list[RouteCandidate], observations: dict[str, PageObservation],
               records: dict[str, URLRecord], config: Config, budget: int) -> list[str]:
    """Second-round samples for families whose samples disagreed or were too few."""
    queues = []
    for c in sorted(candidates, key=lambda c: (-len(c.member_ids), c.pattern)):
        if judge(c.member_ids, observations, records, config).wants_more:
            queues.append([u for u in c.sample_ids + c.extra_sample_ids if u not in observations][: config.extra_samples])
    return round_robin(queues, budget)
