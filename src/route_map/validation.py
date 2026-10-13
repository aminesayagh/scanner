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

After validation, families keep being sampled while their pages show templates not seen yet anywhere on the
site (plan_templates), so a blog whose posts use many layouts gets each layout found, as far as the budget
allows, while one whose posts share a layout stops early.
"""

import hashlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from .features import ROLE_TYPES
from .models import Config, PageObservation, RouteCandidate, URLRecord
from .normalization import display_path, normalize


def unusable_reason(obs: PageObservation, record: URLRecord) -> str | None:
    if obs.fetch_result != "ok":
        return obs.fetch_result
    if obs.final_url and normalize(obs.final_url)["crawl_url"] != record.crawl_url:
        return "redirected"
    return None


def usable_pages(observations: Iterable[PageObservation], records: dict[str, URLRecord]) -> dict[str, PageObservation]:
    """Checked pages that can hold a template: fetched, with a skeleton, not an error page or a redirect."""
    return {o.url_id: o for o in observations
            if o.url_id in records and o.features and unusable_reason(o, records[o.url_id]) is None}


def sample(uid: str, obs: PageObservation, record: URLRecord) -> tuple[str, str, set, frozenset]:
    """A page as `clusters` takes it."""
    return uid, display_path(record.crawl_url), set(obs.features), frozenset(set(obs.structured_data_types) & ROLE_TYPES)


def site_clusters(usable: dict[str, PageObservation], records: dict[str, URLRecord], config: Config) -> list[list[str]]:
    """The site's templates: usable pages grouped by skeleton, whatever their URL (see templates.py)."""
    return clusters([sample(u, o, records[u]) for u, o in usable.items()], config.similarity_threshold)


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def clusters(samples: list[tuple[str, str, set, frozenset]], threshold: float) -> list[list[str]]:
    """Samples (id, URL path, structure, role types) in groups, largest first. Each joins the group it overlaps
    most on average, among those whose role types match and whose every member it overlaps by `threshold` (so a
    page like two pages that are unlike each other does not chain them into one group). Taken in path order,
    then id: the id holds the host, and the groups must not change with it, nor with the input order."""
    groups: list[tuple[frozenset, list]] = []
    for uid, path, feats, roles in sorted(samples, key=lambda s: (s[1], s[0])):
        best, best_score = None, threshold
        for g in groups:
            if g[0] != roles:
                continue
            scores = [jaccard(feats, f) for _, _, f in g[1]]
            score = sum(scores) / len(scores)
            if min(scores) >= threshold and score >= best_score:
                best, best_score = g, score
        if best is None:
            groups.append((roles, [(uid, path, feats)]))
        else:
            best[1].append((uid, path, feats))
    ordered = sorted((g[1] for g in groups), key=lambda m: (-len(m), m[0][1], m[0][0]))
    return [[u for u, _, _ in m] for m in ordered]


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
    groups = clusters([sample(u, observations[u], records[u]) for u in usable], config.similarity_threshold)
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


def plan_samples(candidates: list[RouteCandidate], observed: set[str], budget: int, per_family: int,
                 skip: Collection[str] = ()) -> list[str]:
    """First-round samples, one per family in turn (largest families first) so no family takes the whole
    budget. Pages already fetched during discovery count as samples and cost nothing; pages in `skip` (requested
    already, not HTML) are not planned again."""
    queues = []
    for c in sorted(candidates, key=lambda c: (-len(c.member_ids), c.pattern)):
        have = len(observed & set(c.member_ids))
        queues.append([u for u in c.sample_ids if u not in observed and u not in skip][: max(0, per_family - have)])
    return round_robin(queues, budget)


def plan_extra(candidates: list[RouteCandidate], observations: dict[str, PageObservation],
               records: dict[str, URLRecord], config: Config, budget: int, skip: Collection[str] = ()) -> list[str]:
    """Second-round samples for families whose samples disagreed or were too few (`skip` as in plan_samples)."""
    queues = []
    for c in sorted(candidates, key=lambda c: (-len(c.member_ids), c.pattern)):
        if judge(c.member_ids, observations, records, config).wants_more:
            queues.append([u for u in c.sample_ids + c.extra_sample_ids
                           if u not in observations and u not in skip][: config.extra_samples])
    return round_robin(queues, budget)


def template_queues(candidates: list[RouteCandidate], records: list[URLRecord], observations: dict[str, PageObservation],
                    config: Config, state: dict[str, tuple[int, int, int]], skip: Collection[str] = ()
                    ) -> tuple[list[tuple[RouteCandidate, list[str]]], list[str]]:
    """Families still showing new templates, each with its unchecked members in fetch order, and the unchecked
    URLs in no family (neither in `skip`). Updates `state`, the caller's {pattern: (templates touched, quiet, checked)}: `quiet`
    counts checked pages since the family last touched a site-wide template it had not touched before; at
    `saturation_window` the family is saturated. A family validated on one template without exceptions is
    saturated from the start. Calling it again without new observations changes nothing."""
    by_id = {r.id: r for r in records}
    usable = usable_pages(observations.values(), by_id)
    home = {u: n for n, members in enumerate(site_clusters(usable, by_id, config)) for u in members}
    window, out, grouped = config.saturation_window, [], set()
    for c in sorted(candidates, key=lambda c: (-len(c.member_ids), c.pattern)):
        grouped.update(c.member_ids)
        checked = sum(u in observations for u in c.member_ids)
        touched = len({home[u] for u in c.member_ids if u in home})
        if c.pattern not in state:
            outcome = judge(c.member_ids, observations, by_id, config)
            settled = outcome.kind == "validated" and len(outcome.templates) == 1 and not outcome.exceptions
            state[c.pattern] = (touched, window if settled else 0, checked)
        else:
            before, quiet, seen = state[c.pattern]
            state[c.pattern] = (touched, 0 if touched > before else quiet + checked - seen, checked)
        if state[c.pattern][1] >= window:
            continue
        # by a hash of the path, not the id: the id holds the host, so the order would change with it
        rest = sorted(c.member_ids, key=lambda u: (hashlib.sha1(display_path(by_id[u].crawl_url).encode()).hexdigest(), u))
        queue = [u for u in dict.fromkeys(c.sample_ids + c.extra_sample_ids + rest) if u not in observations and u not in skip]
        if queue:
            out.append((c, queue))
    single = [r.id for r in records if r.id not in grouped and r.id not in observations and r.id not in skip]
    return out, single


def plan_templates(candidates: list[RouteCandidate], records: list[URLRecord], observations: dict[str, PageObservation],
                   config: Config, state: dict[str, tuple[int, int, int]], budget: int, skip: Collection[str] = ()) -> list[str]:
    """The next round of template sampling: up to `saturation_window` unchecked members of each family still
    showing new templates, then each unchecked URL in no family, taken in turn."""
    families, single = template_queues(candidates, records, observations, config, state, skip)
    return round_robin([q[: config.saturation_window] for _, q in families] + [[u] for u in single], budget)
