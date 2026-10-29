import re

import anthropic

from config import CLAUDE_MODEL
from db import canonical_content_id


SUMMARY_CONTEXT = """\
You write short introductions that give social media readers a real sense of a piece of
writing, and a reason to go read it.

- Lead with substance. Say concretely what the piece covers. A mini-summary, not a teaser.
- Warm and inviting. Interested in the material, never breathless about it.
- Earn the reader's curiosity with a specific detail, question, or claim from the piece
  itself. Not with adjectives about how good or important it is.
- Third person or no person at all. Never write as the author, and never invent
  opinions, memories, or first-person claims.
- No sales pitch, no clickbait, no all-caps, no "you won't believe."

These posts are tagged as AI-assisted.
"""

LINKEDIN_PROMPT_TEMPLATE = """\
Write a brief LinkedIn intro to this piece of content.

Content title: {title}
Source: {source}
Description: {description}

Requirements:
- One short paragraph. 40-60 words. That's it.
- Summarize what the piece actually covers, then give one light reason it's worth a read.
- Avoid em-dashes. Use a comma, period, or restructure the sentence instead.
- No first person. Do not write as the author or reference their experience.
- This article may have been published months or years ago, it's being resurfaced from the archive, not freshly written. Do NOT imply recency: no "new piece," "latest piece," "just published," "recently wrote," "this weekend," "this week," or similar. Write as if referencing an existing piece, not announcing one.
- Keep the enthusiasm restrained for this audience. A concrete detail from the piece does the work. No "fascinating," "must-read," "you should read this," or similar filler praise.
- Vary the sentence structure. Do not default to a fixed opener pattern.
- No link in the post body, it will go in the first comment.
- End with 3-5 relevant hashtags drawn from the content's actual topic.
- Final line, exactly as written: [Post written by AI Promotion Engine — article is all human]
- Output the post text only, no surrounding explanation.
"""


FACEBOOK_PROMPT_TEMPLATE = """\
Write a brief Facebook intro to this piece of content.

Content title: {title}
Source: {source}
Description: {description}

Requirements:
- A mini-summary with a light, inviting touch. Interested and friendly, not a sales pitch.
- No em-dashes. Use a comma, period, or just cut the clause instead.
- Tell people what the piece is actually about. Summarize the substance, don't just tease it.
- No first person. Do not write as the author or reference their experience.
- This article may have been published months or years ago, it's being resurfaced from the archive, not freshly written. Do NOT imply recency: no "new piece," "latest piece," "just published," "recently wrote," "this weekend," "this week," or similar. Write as if referencing an existing piece, not announcing one.
- Gentle hype only, and only the kind the piece earns: surface a specific hook from it and let that do the persuading. Never inflate. No "mind-blowing," "everyone should read this," "you have to see this," or similar.
- Vary the structure. Could open with what the piece covers, a question it answers, a surprising detail from it, or a direct statement of the topic.
- 100-200 words total.
- No link in the post body, it will go in the first comment.
- 3-4 relevant hashtags leaning toward the content's actual topic. Spell each one out in
  full, using established tags people already follow: #ArtificialIntelligence, not #AI;
  #MachineLearning, not #ML. No abbreviations or invented tags.
- Final line, exactly as written: [Post written by AI Promotion Engine — article is all human]
- This post will be copy-pasted and posted manually. Write it ready to post as-is.
- Output the post text only, no surrounding explanation.
"""

BLUESKY_PROMPT_TEMPLATE = """\
Write a brief Bluesky intro to this piece of content.

Content title: {title}
Source: {source}
URL: {url}
Description: {description}

Requirements:
- One or two sentences saying what the piece is about, with a light, curious tone. No hard sell.
- No em-dashes. Use a comma, period, or just cut the clause instead.
- No first person. Do not write as the author or reference their experience.
- This article may have been published months or years ago, it's being resurfaced from the archive, not freshly written. Do NOT imply recency: no "new piece," "latest piece," "just published," "recently wrote," "this weekend," "this week," or similar. Write as if referencing an existing piece, not announcing one.
- Include the link.
- End with 2-3 relevant hashtags from the content's topic, plus #AIPromoted.
- The entire post including the URL must be under 280 characters total.
- Keep the body text short to leave room for the URL and hashtags.
- Output the post text only, no surrounding explanation.
"""


BLUESKY_LIMIT = 300  # Bluesky's actual ceiling; the prompt asks for 280 to leave headroom

_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _drop_link(text: str, url: str) -> str:
    """Remove any line linking the article; the link goes in the first comment instead.

    Facebook shows link posts to fewer people, so the post body has to stay link-free
    even if the model adds the URL anyway.
    """
    target = canonical_content_id(url)
    kept = [
        line for line in text.split("\n")
        if not any(canonical_content_id(found.rstrip(".,);:")) == target for found in _URL_RE.findall(line))
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _enforce_bluesky_limit(text: str, url: str) -> str:
    if len(text) <= BLUESKY_LIMIT:
        return text

    url_pos = text.find(url)
    if url_pos == -1:
        return text[:BLUESKY_LIMIT - 3] + "..."

    suffix = text[url_pos:]
    available = BLUESKY_LIMIT - len(suffix) - 1  # -1 for the space before suffix
    if available <= 3:
        return suffix[:BLUESKY_LIMIT]

    truncated = text[:available - 3].rstrip() + "..."
    return truncated + " " + suffix


_PROMPT_TEMPLATES = {
    "linkedin": LINKEDIN_PROMPT_TEMPLATE,
    "bluesky": BLUESKY_PROMPT_TEMPLATE,
    "facebook": FACEBOOK_PROMPT_TEMPLATE,
}


def write_post(content: dict, config: dict, platform: str) -> str:
    """Draft the post for one platform.

    Deliberately per-platform: the caller picks a different article for each platform,
    so drafting all three together produced two drafts about the wrong article and
    tripled the writer's API calls.
    """
    template = _PROMPT_TEMPLATES.get(platform)
    if template is None:
        raise RuntimeError(f"No post template for platform {platform!r}.")

    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    prompt = template.format(
        title=content["title"],
        source=content["source"],
        url=content["url"],
        description=(content.get("description") or "")[:2500],
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SUMMARY_CONTEXT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    if platform == "facebook":
        text = _drop_link(text, content["url"])

    if platform == "bluesky":
        text = _enforce_bluesky_limit(text, content["url"])
        if "#AIPromoted" not in text:
            text = _enforce_bluesky_limit(text.rstrip() + " #AIPromoted", content["url"])

    return text
