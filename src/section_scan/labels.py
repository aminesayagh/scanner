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


# --- page level ------------------------------------------------------------------------------------------

PAGE_REGISTRY_VERSION = "1"

INDUSTRIES: dict[str, dict] = {
    "software_it": {"what": "Software products, SaaS, apps, developer tools, IT services, freelance or agency software development."},
    "marketing_creative_agency": {"what": "Marketing, advertising, branding, design or web agencies selling creative and digital services."},
    "ecommerce_retail": {"what": "Selling consumer goods online or in shops, general retail.", "not_for": "Clothing and beauty brands (fashion_beauty)."},
    "fashion_beauty": {"what": "Clothing, accessories, luxury fashion, cosmetics, beauty and personal care brands."},
    "finance_insurance": {"what": "Banking, payments, fintech, investment, accounting, insurance."},
    "health_medical": {"what": "Healthcare providers, clinics, medical devices, pharmacy, health tech."},
    "wellness_fitness": {"what": "Gyms, sport, yoga, coaching for wellbeing, spas."},
    "education": {"what": "Schools, universities, online courses, training, tutoring, edtech."},
    "real_estate": {"what": "Property sale and rental, agencies, property management, real-estate development."},
    "construction_home": {"what": "Construction, architecture, renovation, interior design, home services and trades."},
    "hospitality_travel": {"what": "Hotels, travel agencies, tourism, airlines, restaurants as places to visit.", "not_for": "Food products and drinks brands (food_beverage)."},
    "food_beverage": {"what": "Food and drink products, producers, delivery, catering."},
    "legal_consulting": {"what": "Law firms, business and management consulting, recruitment, professional services.", "not_for": "Software or creative agencies."},
    "manufacturing_industrial": {"what": "Manufacturing, industrial equipment, logistics, supply chain, B2B hardware."},
    "automotive_transport": {"what": "Cars, mobility, transport services, vehicle dealers and repair."},
    "media_entertainment": {"what": "News, publishing, music, film, gaming, events and entertainment."},
    "arts_photography": {"what": "Artists, photographers, illustrators, galleries showing creative work as art.", "not_for": "Agencies selling design services to businesses (marketing_creative_agency)."},
    "nonprofit_public": {"what": "Charities, NGOs, associations, government and public services."},
    "energy_environment": {"what": "Energy, utilities, renewables, environmental services, agriculture."},
    "other": {"what": "A clear industry that fits none of the above."},
    "unknown": {"what": "Not enough evidence to tell the industry."},
}

PAGE_SCOPES: dict[str, dict] = {
    "company_site": {
        "what": "The site of a company or agency presenting itself and its offering: who they are, services, work, contact.",
        "not_for": "A page promoting one specific product (product_landing); an individual's own site (personal_portfolio).",
    },
    "personal_portfolio": {
        "what": "An individual's site presenting their own work, skills and experience, to win clients or jobs.",
        "not_for": "A company or agency with a team (company_site).",
    },
    "product_landing": {
        "what": "A page promoting one product, app, plugin or software tool, to get sign-ups, downloads or purchases.",
        "not_for": "A store with many products (online_store).",
    },
    "service_landing": {
        "what": "A page promoting one specific service or offer, to generate leads or bookings.",
        "not_for": "A whole company site covering several services (company_site).",
    },
    "online_store": {"what": "A storefront or catalogue listing many products to buy."},
    "product_detail": {"what": "A single product's page within a store, with price and add-to-cart."},
    "blog_article": {"what": "An article, blog post, news story, or a list of them."},
    "documentation": {"what": "Technical documentation, guides, API references, help center."},
    "event": {"what": "A page for a specific event: conference, launch, webinar, wedding."},
    "other": {"what": "A clear page type that fits none of the above."},
    "unknown": {"what": "Not enough evidence to tell what the page is."},
}

PAGE_INSTRUCTIONS_VERSION = "1"

INDUSTRY_INSTRUCTIONS = {
    "task": "Identify the industry of the business or person behind this web page, given in `page` and `sections`.",
    "decide": "Choose the sector the business operates in: what it sells or does for its customers, not the technology of the website.",
    "evidence": [
        "The title, description and section content are the main evidence.",
        "Section labels (`sections[].label`) summarize what each section is for; the link's domain is a supporting clue.",
    ],
    "when_unsure": "Use `other` for a clear sector not listed, and `unknown` only when the page says too little.",
}

SCOPE_INSTRUCTIONS = {
    "task": "Identify what kind of page this is, given in `page` and `sections`.",
    "decide": "Choose the page type that matches its overall purpose for a visitor: what the page as a whole is built to do.",
    "evidence": [
        "The mix and order of sections matter: e.g. hero, services, portfolio and contact suggest a company or personal site; one product's features and pricing suggest a product landing page.",
        "Whether the site speaks as a person (I) or a company (we) helps separate personal_portfolio from company_site.",
    ],
    "when_unsure": "Use `other` for a clear page type not listed, and `unknown` only when the page says too little.",
}
