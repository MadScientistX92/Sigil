"""Jurisdiction rules for AI-generated content labeling.

Each rule says which controls a piece of AI output needs in a given jurisdiction.
This is engineering guidance, not legal advice. Sources are cited per rule and the
rules are versioned so a customer's audit log records which rule set was applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date

RULESET_VERSION = "2026-11-01"

# Controls Sigil can apply or check
C2PA = "c2pa_manifest"            # signed, machine-readable provenance metadata
WATERMARK = "invisible_watermark"  # imperceptible signal in the pixels/audio
VISIBLE = "visible_label"          # human-visible "AI-generated" label
DETECTOR = "public_detector"       # free public tool to check content
NOTICE = "interaction_notice"      # tell users they are dealing with AI
TEXT_MARK = "text_mark"            # machine-readable marking of text output
IDENTIFIER = "unique_identifier"   # per-item ID tying content to a provenance record

MODALITIES = ("image", "video", "audio", "text")
MEDIA = ("image", "video", "audio")
CONTROL_MODALITIES = {C2PA: MEDIA, WATERMARK: MEDIA, TEXT_MARK: ("text",)}


@dataclass(frozen=True)
class Rule:
    id: str
    jurisdiction: str
    law: str
    who: str
    modalities: tuple[str, ...]
    controls: tuple[str, ...]
    effective: date
    summary: str
    applies_if: str = ""            # human-readable trigger condition
    deepfake_only: bool = False     # only realistic content depicting people/events
    min_monthly_users: int = 0      # size threshold, 0 = none
    grace_until: date | None = None
    penalty: str = ""
    sources: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["effective"] = self.effective.isoformat()
        d["grace_until"] = self.grace_until.isoformat() if self.grace_until else None
        return d


RULES: tuple[Rule, ...] = (
    Rule(
        id="eu.art50.2",
        jurisdiction="eu",
        law="EU AI Act, Article 50(2)",
        who="provider",
        modalities=("image", "video", "audio", "text"),
        controls=(C2PA, WATERMARK, TEXT_MARK, IDENTIFIER),
        effective=date(2026, 8, 2),
        grace_until=date(2026, 12, 2),
        summary=("Outputs of generative AI systems must be marked in a machine-readable "
                 "format and be detectable as AI-generated. The Code of Practice expects "
                 "layered marking (signed metadata plus an imperceptible watermark)."),
        applies_if="You place a generative AI system on the EU market or its output is used in the EU.",
        penalty="Up to EUR 15M or 3% of worldwide annual turnover.",
        sources=("https://digital-strategy.ec.europa.eu/en/faqs/code-practice-transparency-ai-generated-content",),
    ),
    Rule(
        id="eu.art50.1",
        jurisdiction="eu",
        law="EU AI Act, Article 50(1)",
        who="provider",
        modalities=("text", "audio"),
        controls=(NOTICE,),
        effective=date(2026, 8, 2),
        summary="People must be told they are interacting with an AI system (chatbots, voice agents).",
        applies_if="Your AI system interacts directly with people.",
        penalty="Up to EUR 15M or 3% of worldwide annual turnover.",
        sources=("https://digital-strategy.ec.europa.eu/en/faqs/code-practice-transparency-ai-generated-content",),
    ),
    Rule(
        id="eu.art50.4",
        jurisdiction="eu",
        law="EU AI Act, Article 50(4)",
        who="deployer",
        modalities=("image", "video", "audio"),
        controls=(VISIBLE,),
        effective=date(2026, 8, 2),
        deepfake_only=True,
        summary="Deployers publishing deepfakes must clearly disclose that the content is AI-generated or manipulated.",
        applies_if="You publish realistic synthetic content depicting real people, places or events.",
        penalty="Up to EUR 15M or 3% of worldwide annual turnover.",
        sources=("https://digital-strategy.ec.europa.eu/en/faqs/code-practice-transparency-ai-generated-content",),
    ),
    Rule(
        id="ca.sb942",
        jurisdiction="ca",
        law="California AI Transparency Act (SB 942, as amended by AB 853)",
        who="provider",
        modalities=("image", "video", "audio"),
        controls=(C2PA, WATERMARK, IDENTIFIER, DETECTOR, VISIBLE),
        effective=date(2026, 8, 2),
        min_monthly_users=1_000_000,
        summary=("Covered providers must embed a latent disclosure (provider, system and version, "
                 "timestamp, unique ID), offer users an optional manifest (visible) disclosure, and "
                 "run a free public AI detection tool. Licensees must keep the latent disclosure intact."),
        applies_if="Your generative AI system has 1M+ monthly users and is publicly accessible in California. Pending SB 1000 may remove the threshold.",
        penalty="USD 5,000 per violation per day.",
        sources=("https://calmatters.digitaldemocracy.org/bills/ca_202520260ab853",),
    ),
    Rule(
        id="in.itrules.sgi",
        jurisdiction="in",
        law="India IT (Intermediary Guidelines) Amendment Rules, 2026",
        who="intermediary",
        modalities=("image", "video", "audio"),
        controls=(VISIBLE, C2PA, IDENTIFIER),
        effective=date(2026, 2, 20),
        deepfake_only=True,
        summary=("Intermediaries that enable creating synthetically generated information that appears "
                 "real must label it prominently and embed permanent metadata or a unique identifier "
                 "where technically feasible, and must not let users strip it."),
        applies_if="Your platform lets users create or share realistic AI-generated audio, images or video in India.",
        penalty="Loss of safe-harbour protection under Section 79 of the IT Act.",
        sources=("https://www.hoganlovells.com/en/publications/india-introduces-mandatory-labelling-for-ai-and-3hour-takedown-for-illegal-content",),
    ),
    Rule(
        id="kr.aibasic.31",
        jurisdiction="kr",
        law="Korea AI Basic Act (Framework Act on AI)",
        who="provider",
        modalities=("image", "video", "audio", "text"),
        controls=(NOTICE, WATERMARK, C2PA, TEXT_MARK),
        effective=date(2026, 1, 22),
        grace_until=date(2027, 1, 22),
        summary=("Generative AI services must notify users in advance and mark outputs as AI-generated; "
                 "an invisible watermark is acceptable for ordinary content."),
        applies_if="You offer a generative AI service to users in Korea.",
        penalty="Administrative fines (suspended during the one-year grace period).",
        sources=("https://www.eweek.com/news/south-korea-ai-basic-act-labeling-law/",),
    ),
    Rule(
        id="kr.aibasic.31.deepfake",
        jurisdiction="kr",
        law="Korea AI Basic Act (Framework Act on AI)",
        who="provider",
        modalities=("image", "video", "audio"),
        controls=(VISIBLE,),
        effective=date(2026, 1, 22),
        grace_until=date(2027, 1, 22),
        deepfake_only=True,
        summary="Realistic content that is hard to tell from reality must carry a clearly visible notice.",
        applies_if="Your output is realistic enough to be mistaken for real people or events.",
        penalty="Administrative fines (suspended during the one-year grace period).",
        sources=("https://www.eweek.com/news/south-korea-ai-basic-act-labeling-law/",),
    ),
)

JURISDICTIONS = {
    "eu": "European Union",
    "ca": "California, USA",
    "in": "India",
    "kr": "South Korea",
}


@dataclass
class Context:
    """What we know about the content and the company producing it."""
    modality: str = "image"
    jurisdictions: tuple[str, ...] = ("eu", "ca", "in", "kr")
    deepfake: bool = False          # realistic depiction of real people/places/events
    monthly_users: int = 0
    role: str = "provider"           # provider | deployer | intermediary


def applicable_rules(ctx: Context, today: date | None = None) -> list[Rule]:
    """Rules that bind for this content today (or that are within 90 days of binding)."""
    today = today or date.today()
    out = []
    for r in RULES:
        if r.jurisdiction not in ctx.jurisdictions:
            continue
        if ctx.modality not in r.modalities:
            continue
        if r.deepfake_only and not ctx.deepfake:
            continue
        if r.min_monthly_users and ctx.monthly_users < r.min_monthly_users:
            continue
        out.append(r)
    return out


def required_controls(ctx: Context, today: date | None = None) -> dict[str, list[str]]:
    """Map each required control to the rule IDs that demand it."""
    need: dict[str, list[str]] = {}
    for r in applicable_rules(ctx, today):
        for c in r.controls:
            if ctx.modality in CONTROL_MODALITIES.get(c, MODALITIES):
                need.setdefault(c, []).append(r.id)
    return need


def rule_status(rule: Rule, today: date | None = None) -> str:
    today = today or date.today()
    if today < rule.effective:
        return f"upcoming ({(rule.effective - today).days} days)"
    if rule.grace_until and today < rule.grace_until:
        return f"in force, grace period ends {rule.grace_until.isoformat()} ({(rule.grace_until - today).days} days)"
    return "in force"
