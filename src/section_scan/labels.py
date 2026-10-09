"""Section label registry, shared by the Jev requests and the confirmation interface.

Bump REGISTRY_VERSION whenever a label or definition changes: stored proposals made with another version
are not reused.
"""

REGISTRY_VERSION = "1"

# Each definition is sent to Jev as the label's criterion and shown to the reviewer.
LABELS: dict[str, dict] = {
    "header": {
        "what": "The site-wide top bar: logo, main navigation, account or language links, often a call-to-action button.",
        "not_for": "A large introductory banner with a headline (hero).",
    },
    "footer": {
        "what": "The site-wide bottom area: secondary links, legal notices, copyright, social links, newsletter sign-up.",
        "not_for": "A section asking the visitor to get in touch (contact) or to act (cta).",
    },
    "hero": {
        "what": "The opening banner of the page: the main headline and promise, usually with a short supporting line and a primary button or image.",
        "not_for": "Later sections that repeat a call to action (cta).",
    },
    "about": {
        "what": "Who the person or company is: background, mission, values, story, team.",
        "not_for": "What they offer to do for clients (services).",
    },
    "services": {
        "what": "What is offered: a list or description of services, capabilities, features or plans the visitor can buy or request.",
        "not_for": "Who the provider is (about); finished projects (portfolio or case_studies).",
    },
    "portfolio": {
        "what": "A collection of projects or clients: several items, each briefly identified (name, date, short line).",
        "not_for": "A detailed narrative of one or a few projects (case_studies); images shown mainly for their look (gallery).",
        "examples": ["A list of client projects with dates", "A grid of project cards with names"],
    },
    "case_studies": {
        "what": "Detailed project narratives: the problem, the work done and the outcome, for one or a few projects.",
        "not_for": "A short list of many projects (portfolio).",
    },
    "testimonials": {
        "what": "What clients or users say: quotes, reviews or endorsements attributed to people.",
        "not_for": "The provider describing its own work, even if the heading sounds like stories.",
    },
    "faq": {
        "what": "Frequently asked questions with their answers.",
        "not_for": "A single question used as a heading for other content.",
    },
    "gallery": {
        "what": "Primarily visual content: a set of images or screenshots shown for their look, with little text.",
        "not_for": "Projects identified by name and description (portfolio).",
    },
    "contact": {
        "what": "How to reach the provider: a contact form, email, phone, address, booking link.",
        "not_for": "A short prompt to act without contact details (cta).",
    },
    "cta": {
        "what": "A call to action: a short persuasive block with a button inviting the visitor to start, sign up, buy or talk.",
        "not_for": "A section giving contact details or a form (contact); the opening banner (hero).",
    },
    "other": {
        "what": "Understandable content whose purpose fits none of the other labels (e.g. pricing table, blog list, logos of partners, newsletter).",
        "not_for": "Content too thin or unclear to understand (unknown).",
    },
    "unknown": {
        "what": "Not enough evidence to tell what the section is for.",
        "not_for": "Clear content that simply fits no other label (other).",
    },
}

INSTRUCTIONS_VERSION = "1"

INSTRUCTIONS = {
    "task": "Classify the purpose of one section of a web page, given in `section`, within the page described in `page`.",
    "decide": "Choose the label that matches the section's dominant purpose: what a visitor is meant to learn or do there.",
    "evidence": [
        "Headings, paragraphs, list items and their grouping are the main evidence.",
        "Buttons and links, the DOM id (`section.dom_id`), the layout role and the position in the page are supporting clues only; do not let them override clear content.",
        "Image entries carry the image's alt text.",
    ],
    "when_unsure": "Use `other` for clear content that fits no label, and `unknown` only when the content is too thin to tell.",
}
