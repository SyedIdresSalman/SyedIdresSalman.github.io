"""100% free auto blog: find -> verify -> write -> image -> publish to GitHub Pages (Jekyll)."""
import os, re, json, glob, html, difflib, datetime
from urllib.parse import urlparse, quote_plus
import requests, feedparser
from bs4 import BeautifulSoup

E = os.environ.get
NICHE = E("NICHE", "AI tools for students and freelancers")
GEMINI_KEY = E("GEMINI_API_KEY", "")
GEMINI_MODEL = E("GEMINI_MODEL", "gemini-2.5-flash")
PIXABAY_KEY = E("PIXABAY_API_KEY", "")   # optional; Openverse needs no key
BOT_VERSION = 2
STATUS = E("PUBLISH_STATUS", "publish")    # "publish" = live right after checks; "review" = save in _review/
MAX_POSTS = int(E("MAX_POSTS", "1"))
MIN_SOURCES = int(E("MIN_SOURCES", "2"))
STATE = "posted.json"
UA = {"User-Agent": "Mozilla/5.0 (blog-bot)"}

FEEDS = [
    "https://openai.com/blog/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://huggingface.co/blog/feed.xml",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://github.blog/ai-and-ml/feed/",
    # official company blogs
    "https://deepmind.google/blog/rss.xml",
    "https://blogs.microsoft.com/ai/feed/",
    "https://blogs.nvidia.com/feed/",
    "https://aws.amazon.com/blogs/machine-learning/feed/",
    # news sites
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "https://www.wired.com/feed/tag/ai/latest/rss",
    "https://arstechnica.com/ai/feed/",
    "https://www.zdnet.com/topic/artificial-intelligence/rss.xml",
    "https://www.marktechpost.com/feed/",
    # expert blogs and community
    "https://simonwillison.net/atom/everything/",
    "https://hnrss.org/newest?q=AI+tool&points=50",
]
KEYWORDS = [k.strip().lower() for k in E(
    "KEYWORDS", "ai,gpt,gemini,claude,llm,model,agent,tool,copilot,app").split(",")]

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {"posted": []}

def domain(u): return urlparse(u).netloc.lower().replace("www.", "")

def page_text(url, limit=7000):
    r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "aside"]): t.decompose()
    text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
    return re.sub(r"\s+", " ", text)[:limit]

def candidates(seen):
    items = []
    for f in FEEDS:
        try:
            for e in feedparser.parse(f).entries[:15]:
                t = e.get("title", "")
                if e.get("link") in seen: continue
                if any(k in t.lower() for k in KEYWORDS):
                    items.append((e.get("published_parsed") or (0,), t, e["link"]))
        except Exception as ex:
            print("feed error", f, ex)
    items.sort(reverse=True)
    return [(t, l) for _, t, l in items]

def corroboration(title, origin_link):
    url = f"https://news.google.com/rss/search?q={quote_plus(title)}&hl=en-US&gl=US&ceid=US:en"
    outlets = {domain(origin_link)}
    for e in feedparser.parse(url).entries[:15]:
        ratio = difflib.SequenceMatcher(None, title.lower(), e.title.lower()).ratio()
        src = domain(e.get("source", {}).get("href", ""))
        if ratio > 0.45 and src: outlets.add(src)
    return outlets

def gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8}}
    r = requests.post(url, json=body, timeout=120); r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])

WRITE = """You are a human blogger writing for readers interested in: {niche}.
Write an original, easy-to-read blog post that is well optimized for search, using ONLY the facts in SOURCE.
Accuracy rules:
- Every claim must be supported by SOURCE. If it is not in SOURCE, leave it out. No invented numbers, quotes or dates.
- Never copy phrases from SOURCE; rewrite fully in your own words.
Readability rules:
- 700-1000 words. Grade 8 reading level, active voice, plain words. Paragraphs of 1-3 sentences.
- Mix short and long sentences, add a clear opinion or practical takeaway, speak to the reader as "you".
- No filler such as "In today's fast-paced world", "delve", "game-changer", "revolutionize", "unlock", "landscape".
SEO structure (use HTML, no <h1>):
- Pick one focus keyword (the main thing people would search for). Use it naturally in the first paragraph, one <h2>, and the meta description. No keyword stuffing.
- Open with 2-3 sentences that say what happened and why it matters to the reader.
- Then <h2>Key takeaways</h2> with a <ul> of 3-4 short bullets.
- Then 3-5 sections with descriptive <h2> headings (some phrased as questions people search), using <ul> or <ol> where helpful.
- If SOURCE allows, include a practical <h2>How to use it</h2> or <h2>Who is it for?</h2> section.
- End with <h2>FAQ</h2> with 2-3 question-and-answer pairs (<h3> question, <p> answer) drawn only from SOURCE.
- Internal links: if one of EXISTING POSTS is truly relevant, link to it once or twice with natural anchor text, e.g. <a href="/2026/01/02/slug.html">anchor</a>. Skip if none fit.
Return JSON: {{"title": str (under 60 chars, includes the focus keyword, no clickbait), "slug": str (lowercase-hyphens, max 6 words), "meta_description": str (120-155 chars, includes the focus keyword, states the benefit), "focus_keyword": str, "html": str, "tags": [3-5 str], "image_query": str (concrete real-world photo subject, 2-4 words), "image_alt": str (describes the photo, under 120 chars)}}
EXISTING POSTS:
{links}
SOURCE ({url}):
{source}"""

CHECK = """Fact-check. For each factual claim in ARTICLE, check it is supported by SOURCE.
Return JSON: {{"unsupported_claims": [str]}} (empty list if everything is supported).
SOURCE: {source}
ARTICLE: {article}"""

def too_similar(article_html, source, n=8):
    words = lambda s: re.findall(r"\w+", s.lower())
    src, art = words(source), words(BeautifulSoup(article_html, "html.parser").get_text(" "))
    grams = {tuple(src[i:i+n]) for i in range(len(src) - n)}
    return any(tuple(art[i:i+n]) in grams for i in range(len(art) - n))

def _download(url):
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    if len(r.content) < 15000 or "image" not in r.headers.get("content-type", ""):
        raise ValueError("not a usable image")
    return r.content

def pixabay_image(query):
    """Pixabay: free key, free for commercial use, no attribution required (we still credit)."""
    if not PIXABAY_KEY: return None
    r = requests.get("https://pixabay.com/api/", timeout=25, params={
        "key": PIXABAY_KEY, "q": query, "image_type": "photo", "orientation": "horizontal",
        "safesearch": "true", "per_page": 5})
    for h in r.json().get("hits", []):
        try:
            data = _download(h["largeImageURL"])
        except Exception:
            continue
        credit = (f'Photo by <a href="{h["pageURL"]}" rel="nofollow noopener">{html.escape(h["user"])}</a> on Pixabay')
        return data, credit, (h.get("tags") or query)
    return None

def openverse_image(query):
    """Openverse: no key needed. Only CC0, public domain and CC BY (reuse allowed, credit required)."""
    r = requests.get("https://api.openverse.org/v1/images/", headers=UA, timeout=30, params={
        "q": query, "license": "cc0,pdm,by", "page_size": 8, "mature": "false",
        "category": "photograph"})
    for h in r.json().get("results", []):
        try:
            data = _download(h["url"])
        except Exception:
            continue
        lic = (h.get("license") or "").upper()
        ver = h.get("license_version") or ""
        title = html.escape(h.get("title") or "Photo")
        who = html.escape(h.get("creator") or "Unknown")
        who_html = f'<a href="{h["creator_url"]}" rel="nofollow noopener">{who}</a>' if h.get("creator_url") else who
        credit = (f'<a href="{h["foreign_landing_url"]}" rel="nofollow noopener">{title}</a> by {who_html}, '
                  f'license: {lic} {ver}'.strip())
        return data, credit, (h.get("title") or query)
    return None

def get_image(query):
    for fn in (pixabay_image, openverse_image):
        try:
            res = fn(query)
            if res: return res
        except Exception as ex:
            print("image source error:", fn.__name__, ex)
    print("no image found; publishing without one")
    return None

def img_ext(data):
    if data[:4] == b"\x89PNG": return "png"
    if data[:4] == b"RIFF": return "webp"
    return "jpg"

def slugify(t): return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]
def clean(s): return s.replace("{{", "{ {").replace("{%", "{ %")
def yq(s): return json.dumps(s)  # safe YAML string

def recent_links(n=6):
    out = []
    for f in sorted(glob.glob("_posts/*.md"), reverse=True)[:n]:
        m = re.match(r"_posts/(\d{4})-(\d{2})-(\d{2})-(.+)\.md", f)
        if not m: continue
        y, mo, d, slug = m.groups()
        title = slug.replace("-", " ")
        try:
            for line in open(f, encoding="utf-8"):
                if line.startswith("title:"):
                    title = json.loads(line.split(":", 1)[1].strip()); break
        except Exception: pass
        out.append(f"- {title}: /{y}/{mo}/{d}/{slug}.html")
    return "\n".join(out) or "(none yet)"

def save_post(post, img, source_link):
    today = datetime.datetime.utcnow()
    slug = slugify(post.get("slug") or post["title"])
    img_md, img_front = "", ""
    if img:
        data, credit, alt = img
        alt = post.get("image_alt") or alt
        ext = img_ext(data)
        os.makedirs("assets/img", exist_ok=True)
        open(f"assets/img/{slug}.{ext}", "wb").write(data)
        img_front = f"image: /assets/img/{slug}.{ext}\n"
        img_md = (f"<img src=\"{{{{ '/assets/img/{slug}.{ext}' | relative_url }}}}\" alt=\"{html.escape(alt)}\" "
                  f"style=\"max-width:100%\" loading=\"lazy\">\n"
                  f"<p><em>{credit}</em></p>\n\n")
    front = ("---\nlayout: post\n"
             f"title: {yq(post['title'])}\n"
             f"date: {today:%Y-%m-%d %H:%M:%S} +0000\n"
             f"description: {yq(post['meta_description'][:158])}\n"
             f"tags: {json.dumps(post['tags'])}\n" + img_front + "---\n\n")
    src = f"\n<h3>Source</h3>\n<p><a href=\"{source_link}\" rel=\"nofollow noopener\">{domain(source_link)}</a></p>\n"
    folder = "_posts" if STATUS == "publish" else "_review"
    os.makedirs(folder, exist_ok=True)
    path = f"{folder}/{today:%Y-%m-%d}-{slug}.md"
    open(path, "w", encoding="utf-8").write(front + img_md + clean(post["html"] + src))
    return path

def main():
    state, done = load_state(), 0
    for title, link in candidates(set(state["posted"])):
        if done >= MAX_POSTS: break
        try:
            outlets = corroboration(title, link)
            if len(outlets) < MIN_SOURCES:
                print("skip (not enough independent coverage):", title); continue
            source = page_text(link)
            if len(source) < 800: print("skip (thin source):", title); continue
            post = gemini(WRITE.format(niche=NICHE, url=link, source=source, links=recent_links()))
            if too_similar(post["html"], source):
                print("skip (too close to source):", title); continue
            bad = gemini(CHECK.format(source=source, article=post["html"]))["unsupported_claims"]
            if bad: print("skip (unsupported claims):", bad); continue
            path = save_post(post, get_image(post["image_query"]), link)
            print("OK:", path); state["posted"].append(link); done += 1
        except Exception as ex:
            print("error on", title, "->", ex)
    json.dump(state, open(STATE, "w"), indent=1)

if __name__ == "__main__":
    main()
