import os
import re
import shutil
import json
from pathlib import Path
import sys
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from markdown import Markdown
from markdown.preprocessors import HtmlBlockPreprocessor
from markdown.inlinepatterns import LinkInlineProcessor
from markdown.extensions import Extension
from urllib.parse import urlparse
import html
from markdown.preprocessors import Preprocessor
import unicodedata
import random

# ==============================================================================
# LOGGING UTILITY
# ==============================================================================

def log(msg: str, error: bool = False):
    if error:
        print(f"> #ERROR: {msg}")
    else:
        print(f"> log: {msg}")

# ==============================================================================
# CONFIGURATION AND UTILITIES
# ==============================================================================

# Global configuration flags and default filenames
ENABLE_CORS_THUMBNAIL_FETCH = False
SEARCH_JSON_FILE = "search.json"

# JavaScript snippet for dynamically loading thumbnail cards and favicons via CORS proxies
CARD_JS_html = """
<script>
document.addEventListener("DOMContentLoaded", function() {
    const cards = document.querySelectorAll('.card-container');
    const CACHE_PREFIX = 'card_img_cache_';
    const LOCAL_STORAGE_ITEMS_REMOVE = 30;

    // List of CORS proxies to fetch OpenGraph images from external links safely
    let proxies = [
        (url) => `https://api.microlink.io/?url=${encodeURIComponent(url)}`,
        (url) => `https://corsproxy.io/?${encodeURIComponent(url)}`,
        (url) => `https://crossorigin.me/${encodeURIComponent(url)}`,
        (url) => `https://api.allorigins.win/get?url=${encodeURIComponent(url)}`,
        (url) => `https://api.allorigins.win/raw?url=${encodeURIComponent(url)}`,
        (url) => `https://proxy.cors.sh/${url}`,
        (url) => `https://thingproxy.freeboard.io/fetch/${url}`,
        (url) => `https://api.codetabs.com/v1/proxy?quest=${url}`
    ];

    // Shuffle proxies to distribute load randomly
    function shuffleArray(array) {
        for (let i = array.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [array[i], array[j]] = [array[j], array[i]];
        }
        return array;
    }

    proxies = shuffleArray(proxies);

    // Function to asynchronously load and cache card preview images
    const loadCardData = (card) => {
        const linkElem = card.querySelector('.card-link');
        if (!linkElem) return;

        const imgContainerElem = linkElem.querySelector('.card-image-container');
        const img = imgContainerElem.querySelector('img');
        const link = linkElem.getAttribute('href');

        const showAndCacheImage = (url, src) => {
            if (!src) return;
            img.src = src;
            try {
                localStorage.setItem(CACHE_PREFIX + url, src);
            } catch (e) {
                // Clear old items if localStorage quota is exceeded
                const keys = Object.keys(localStorage).filter(k => k.startsWith(CACHE_PREFIX));
                for (let i = 0; i < Math.min(keys.length, LOCAL_STORAGE_ITEMS_REMOVE); i++) {
                    localStorage.removeItem(keys[i]);
                }
                localStorage.setItem(CACHE_PREFIX + url, src);
            }
        };

        if (img.getAttribute('src') && img.getAttribute('src').trim() !== "") {
            return;
        }

        if (link !== null) {
            const cachedImg = localStorage.getItem(CACHE_PREFIX + link);
            if (cachedImg) {
                img.src = cachedImg;
                return;
            }
        }

        // Try fetching through proxies sequentially until an image is found
        async function tryProxies(url) {
            let imageFound = false;
            for (const proxy of proxies) {
                try {
                    const proxyUrl = proxy(url);
                    const response = await fetch(proxyUrl);
                    if (!response.ok) continue;

                    let imageUrl = null;
                    if (proxyUrl.includes('microlink.io')) {
                        const json = await response.json();
                        imageUrl = json.data?.image?.url;
                    } else if (proxyUrl.includes('allorigins') && proxyUrl.includes('/get?')) {
                        const json = await response.json();
                        const doc = new DOMParser().parseFromString(json.contents, "text/html");
                        imageUrl = doc.querySelector('meta[property="og:image"]')?.content ||
                                   doc.querySelector('meta[name="twitter:image"]')?.content;
                    } else {
                        const html = await response.text();
                        const doc = new DOMParser().parseFromString(html, "text/html");
                        imageUrl = doc.querySelector('meta[property="og:image"]')?.content ||
                                   doc.querySelector('meta[name="twitter:image"]')?.content;
                    }

                    if (imageUrl) {
                        showAndCacheImage(url, imageUrl);
                        imageFound = true;
                        break;
                    }
                } catch (err) {}
            }

            // Fallback to Google favicon service if no OpenGraph image is found
            if (!imageFound) {
                try {
                    const domain = new URL(url).hostname;
                    const faviconUrl = `https://www.google.com/s2/favicons?sz=128&domain=${domain}`;
                    showAndCacheImage(url, faviconUrl);
                } catch (err) {}
            }
        }

        if (link !== null) {
            setTimeout(tryProxies, 150, link);
        }
    };

    // Use Intersection Observer to load card images lazily only when visible in viewport
    const observerOptions = {
        root: null,
        rootMargin: '200px',
        threshold: 0.1
    };

    const observer = new IntersectionObserver((entries, observer) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const card = entry.target;
                loadCardData(card);
                observer.unobserve(card);
            }
        });
    }, observerOptions);

    cards.forEach(card => {
        observer.observe(card);
    });
});
</script>
"""

# JavaScript and HTML markup for the interactive search widget
SEARCH_WIDGET_JS_html = """
<div class="search-container">
    <input type="text" class="search-input" id="search-input" placeholder="search..." autocomplete="off">
    <div class="search-results" id="search-results" style="display: none;"></div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {
    const input = document.getElementById('search-input');
    const resultsContainer = document.getElementById('search-results');
    let searchIndex = [];
    let currentIndex = -1;

    // Fetch the generated search index JSON file asynchronously
    fetch('/search.json')
        .then(response => response.json())
        .then(data => { searchIndex = data.files; });

    // Helper function to strip accents for robust fuzzy/accent-insensitive searching
    function removeAccents(str) {
        return str.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
    }

    input.addEventListener('input', function() {
        const rawQuery = this.value.trim();
        const query = removeAccents(rawQuery);

        resultsContainer.innerHTML = '';
        resultsContainer.style.display = 'none';
        currentIndex = -1;

        if (query.length < 2) return;

        resultsContainer.style.display = 'block';

        const titleMatches = [];
        const contentMatches = [];

        // Filter search index items based on title or content snippets
        searchIndex.forEach(item => {
            const titleLower = removeAccents(item.displayTitle || item.title || "");
            const snippetLower = removeAccents(item.snippet);

            if (titleLower.includes(query)) {
                titleMatches.push(item);
            } else if (snippetLower.includes(query)) {
                contentMatches.push(item);
            }
        });

        const matches = [...titleMatches, ...contentMatches];

        if (matches.length === 0) {
            const div = document.createElement('div');
            div.style.padding = '8px';
            div.style.color = '#666';
            div.textContent = 'No se encontraron resultados';
            resultsContainer.appendChild(div);
            return;
        }

        // Render search result items with text highlighting
        matches.forEach((item, index) => {
            let matchSnippet = "";
            const lowerSnippet = item.snippet.toLowerCase();
            const matchIndex = lowerSnippet.indexOf(query);

            if (matchIndex !== -1) {
                const start = Math.max(0, matchIndex - 40);
                const end = Math.min(item.snippet.length, matchIndex + query.length + 60);
                matchSnippet = (start > 0 ? "..." : "") + item.snippet.substring(start, end) + (end < item.snippet.length ? "..." : "");
            } else {
                matchSnippet = item.snippet.substring(0, 100) + "...";
            }

            let titleText = item.displayTitle || item.title || "";

            if (query.length > 0) {
                const escapedQuery = query.replace('/[-\\/\\^$*+?.()|[\\]{}]/g, '\\\\$&');
                const regex = new RegExp(`(${escapedQuery})`, 'gi');
                matchSnippet = matchSnippet.replace(regex, '<mark>$1</mark>');
                titleText = titleText.replace(regex, '<mark>$1</mark>');
            }

            const div = document.createElement('div');
            div.className = 'search-result-item';
            div.innerHTML = `<a href="${item.url}"><strong>${titleText}</strong></a><p>${matchSnippet}</p>`;

            div.addEventListener('click', function() {
                window.location.href = item.url;
            });

            resultsContainer.appendChild(div);
        });
    });

    // Handle keyboard navigation (Arrow keys, Enter, Escape) inside search results
    function updateActiveItem() {
        const items = resultsContainer.querySelectorAll('.search-result-item');
        items.forEach((item, index) => {
            if (index === currentIndex) {
                item.classList.add('selected');
                item.scrollIntoView({ block: 'nearest' });
            } else {
                item.classList.remove('selected');
            }
        });
    }

    input.addEventListener('keydown', function(e) {
        const items = resultsContainer.querySelectorAll('.search-result-item');
        if (items.length === 0) return;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            currentIndex = (currentIndex + 1) % items.length;
            updateActiveItem();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            currentIndex = (currentIndex - 1 + items.length) % items.length;
            updateActiveItem();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (currentIndex >= 0 && currentIndex < items.length) {
                const link = items[currentIndex].querySelector('a');
                if (link) {
                    window.location.href = link.getAttribute('href');
                }
            }
        }
    });

    // Close search box when clicking outside or pressing Escape
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.search-container')) {
            resultsContainer.innerHTML = '';
            currentIndex = -1;
        }
    });

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            resultsContainer.innerHTML = '';
            currentIndex = -1;
            input.blur();
        }
    });
});
</script>
"""

# JavaScript for a "Go Up" button that appears when scrolling down the page
GO_UP_BUTTON_JS_html = """
<button onclick="window.scrollTo({top: 0, behavior: 'smooth'});" id="go-up-button" class="go-up-button" title="Go up">go up ⬆️</button>
<script>
window.onscroll = function() {
    let btn = document.getElementById("go-up-button");
    if (document.body.scrollTop > 300 || document.documentElement.scrollTop > 300) {
        btn.style.display = "block";
    } else {
        btn.style.display = "none";
    }
};
</script>
"""

# Regular expressions used to detect custom Markdown block patterns (card links, notices, YouTube URLs, GitHub repos)
CARD_LINK_PATTERN = re.compile(r'^\s*>\s*(!\[.*?\]\((.*?)\))?\s*\[(.*?)\]\((.*?)\)\s*(\[(.*?)\]\((.*?)\))?\s*$', re.MULTILINE)
NOTICE_PATTERN = re.compile(r'^\s*>\s*!\s*(\{(.*)\})?\s*.*', re.MULTILINE)
YOUTUBE_PATTERN = re.compile(r"(?<=v=)[^?&#/]+|(?<=be/)[^?&#/]+|(?<=/vi/)[^?&#/]+")
GITHUB_PATTERN = re.compile(r'github\.com\/([^\/]+)\/([^\/]+)')

# ==============================================================================
# HTML ELEMENTS BUILDER UTILITIES
# ==============================================================================

# Helper to construct generic HTML tags with attributes and inner content
def html_tag(tag, content="", attrs=None):
    # Turn attributes dictionary into an HTML attribute string
    attrs_str = "".join(f' {k}="{v}"' for k, v in (attrs or {}).items() if v is not None)
    return f"<{tag}{attrs_str}>{content}</{tag}>"

def html_tag_void(tag, attrs=None):
    # Turn attributes dictionary into a void HTML tag string (tags without closing tags)
    attrs_str = "".join(f' {k}="{v}"' for k, v in (attrs or {}).items() if v is not None)
    return f"<{tag}{attrs_str}>"

# Helper to build HTML image tags
def build_img_tag(src, cls=None, loading="lazy"):
    # Build an image tag with source, class, and lazy loading
    return html_tag_void("img", {"src": src, "class": cls, "loading": loading})

# Helper to build HTML anchor links
def build_link_tag(href, content, target="_blank", rel="noopener noreferrer", cls=None):
    # Build an anchor link tag with security defaults
    return html_tag("a", content, {"href": href, "target": target, "rel": rel, "class": cls})

# ==============================================================================
# CARD GENERATION
# ==============================================================================

# Construct the structured HTML snippet for a rich preview link card
def create_card_html(favicon, img, title, url, extra):
    # Set default values if icons or images are missing
    favicon_src = favicon or ""
    img_src = img or favicon_src

    # Extract the main domain from the URL to display below the title
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    extra = extra or base_url

    # Build individual pieces of the preview card
    img_container_html = html_tag("div", build_img_tag(img_src, loading="lazy"), {"class": "card-image-container"})
    title_elem_html = html_tag("p", title, {"class": "card-title"})

    favicon_elem_html = build_img_tag(favicon_src, cls="card-favicon", loading="lazy")
    base_url_elem_html = html_tag("p", extra, {"class": "card-base-url"})
    base_url_container_html = html_tag("div", f"{favicon_elem_html}{base_url_elem_html}", {"class": "card-base-url-container"})

    # Combine title and URL into a content wrapper, then wrap everything in a link
    card_content_html = html_tag("div", f"{title_elem_html}{base_url_container_html}", {"class": "card-content"})
    card_link_html = build_link_tag(url, f"{img_container_html}{card_content_html}", cls="card-link")

    return html_tag("article", card_link_html, {"class": "card-container"})

# Automatically resolve image previews for recognized services like YouTube or GitHub
def get_autogenerated_image(url):
    # If the link is a YouTube video, use its thumbnail image
    if match := YOUTUBE_PATTERN.search(url):
        return f"https://img.youtube.com/vi/{match.group(0)}/hqdefault.jpg"
    # If the link is a GitHub repository, use its OpenGraph image
    if match := GITHUB_PATTERN.search(url):
        return f"https://opengraph.githubassets.com/1/{match.group(1)}/{match.group(2)}"
    return ""

# ==============================================================================
# MARKDOWN EXTENSION CLASSES
# ==============================================================================

# Markdown preprocessor to transform custom blockquote card syntax into card HTML
class CardLinkProcessor(HtmlBlockPreprocessor):
    def run(self, lines):
        # Join lines together to search for card blockquote patterns
        md_content = "\n".join(lines)
        matches = list(CARD_LINK_PATTERN.finditer(md_content))
        if not matches:
            return lines

        new_content, last_end = [], 0
        for match in matches:
            # Keep text before the match
            new_content.append(md_content[last_end:match.start()])
            # Extract card details like title, URL, image, and extra text
            title, url = match.group(3).strip() if match.group(3) else "", match.group(4).strip() if match.group(4) else ""
            img = match.group(2).strip() if match.group(2) else get_autogenerated_image(url)
            extra = match.group(6).strip() if match.group(6) else ""
            favicon = f"https://www.google.com/s2/favicons?sz=128&domain={urlparse(url).netloc}"

            # Generate the card HTML and add it to the content list
            card_html = create_card_html(favicon, img, title, url, extra)
            new_content.append(f"\n{card_html}\n")
            last_end = match.end()

        new_content.append(md_content[last_end:])
        return "".join(new_content).split('\n')

class CardLinkExtension(Extension):
    def extendMarkdown(self, md):
        # Register the card link preprocessor with Markdown
        md.preprocessors.register(CardLinkProcessor(md), 'cardlink', 75)

# Markdown preprocessor to transform callout/notice blockquotes into styled notice boxes
class NoticeProcessor(HtmlBlockPreprocessor):
    def run(self, lines):
        md_content = "\n".join(lines)
        matches = list(NOTICE_PATTERN.finditer(md_content))
        if not matches:
            return lines

        new_content, last_end = [], 0
        for match in matches:
            new_content.append(md_content[last_end:match.start()])
            block_text, notice_class = match.group(0), match.group(2)

            # Clean the callout syntax from each line
            cleaned_lines = []
            for line in block_text.splitlines():
                cleaned_line = line
                cleaned_line = re.sub(r'^\s*>\!\s?', '', cleaned_line)
                cleaned_line = re.sub(r'^\s*\{.*\}\s?', '', cleaned_line)
                cleaned_lines.append(cleaned_line)

            # Convert the inner content of the notice block into HTML
            content = "\n".join(cleaned_lines).strip()
            inner_html = Markdown(extensions=['toc', 'tables', 'fenced_code', 'footnotes', 'attr_list', 'def_list']).convert(content)

            # Wrap the content in a styled notice container
            notice_html = html_tag("div", f"\n{inner_html}\n", {"class": f"notice {notice_class or ''}"})
            new_content.append(f"\n{notice_html}\n")
            last_end = match.end()

        new_content.append(md_content[last_end:])
        return "".join(new_content).split('\n')

class NoticeExtension(Extension):
    def extendMarkdown(self, md):
        # Register the notice preprocessor with Markdown
        md.preprocessors.register(NoticeProcessor(md), 'notice', 75)

# Markdown preprocessor to automatically turn headers into self-referencing anchor links
class AutoHeaderLinkPreprocessor(Preprocessor):
    def run(self, lines):
        header_pattern = re.compile(r'^(#{1,6})\s+(.+)$')
        new_lines = []
        for line in lines:
            # If the line is a header, turn it into an anchor link pointing to its slug
            if match := header_pattern.match(line):
                hashes, title = match.groups()
                if not title.strip().startswith('['):
                    slug = re.sub(r'[^\w\s-]', '', title.lower()).strip().replace(' ', '-')
                    line = f"{hashes} [{title}](#{slug})"
            new_lines.append(line)
        return new_lines

class AutoHeaderLinkExtension(Extension):
    def extendMarkdown(self, md):
        # Register the auto header link preprocessor with Markdown
        md.preprocessors.register(AutoHeaderLinkPreprocessor(md), 'autoheaderlink', 75)

# ==============================================================================
# FILE PROCESSING HELPERS
# ==============================================================================

# Check if a filename or directory name starts with a dot (hidden files/folders)
def is_hidden(name):
    return name.startswith('.')

# Clean up raw markdown text to generate short search index snippets
def clean_snippet(text):
    # Remove markdown image tags and HTML tags
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = html.unescape(text)
    clean_text = re.sub(r'<[^>]+>', '', text)
    clean_text = re.sub(r'[\s\n\r\t]+', ' ', clean_text)

    # Remove accents to normalize text for search indexing
    clean_text = ''.join(
        c for c in unicodedata.normalize('NFD', clean_text)
        if unicodedata.category(c) != 'Mn'
    )
    return re.sub(r'\s+', ' ', clean_text).strip()

# Parse an individual markdown file into HTML content and Table of Contents (TOC)
def parse_markdown_file(src_file: Path, config):
    # Create a Markdown parser with all required extensions enabled
    markdown_instance = Markdown(extensions=[
        CardLinkExtension(), AutoHeaderLinkExtension(), NoticeExtension(),
        'toc', 'tables', 'fenced_code', 'footnotes', 'attr_list', 'def_list'
    ])
    with open(src_file, 'r', encoding='utf-8') as f:
        markdown_content = f.read()
    # Convert markdown text into HTML and extract TOC
    content_html = markdown_instance.convert(markdown_content)
    return content_html, markdown_instance.toc, markdown_content

# ==============================================================================
# CONVERT SINGLE FILE (MARKDOWN to HTML) HELPERS
# ==============================================================================

# Generate global navigation items from configuration
def generate_header_nav(config):
    nav_items = config.get("header_nav_items", [])
    if not nav_items:
        return ""
    # Create HTML links for each top-level navigation item
    items_html = "".join(html_tag("a", item.get("title", ""), {"href": item.get("href", "#")}) for item in nav_items)
    return html_tag("nav", items_html, {"class": "header-nav"})

# Recursively build the file and folder directory tree for the sidebar
def build_directory_tree(source_dir: Path, current_path: Path, config):
    tree = []
    try:
        # Sort directory entries if configured to do so
        entries = sorted(os.listdir(current_path)) if config.get("navigation_sorted", False) else os.listdir(current_path)
    except OSError:
        return tree

    nav_folders = [Path(f) for f in config.get("navigation_folders", [])]
    attachments_folders = [Path(f) for f in config.get("attachments_folders", [])]

    for entry in entries:
        # Skip hidden files and folders
        if is_hidden(entry):
            continue
        full_path = current_path / entry
        rel_from_source = full_path.relative_to(source_dir)

        # Check if the folder is allowed in navigation and is not an attachment folder
        is_valid = (not nav_folders or any(full_path.is_relative_to(source_dir / f) for f in nav_folders)) and \
                   not any(full_path.is_relative_to(source_dir / f) for f in attachments_folders)

        if is_valid:
            if full_path.is_dir():
                # Recursively process subdirectories
                tree.append({
                    "type": "dir", "name": entry, "rel_path": rel_from_source,
                    "children": build_directory_tree(source_dir, full_path, config)
                })
            elif entry.endswith('.md'):
                # Add markdown files to the tree
                tree.append({"type": "file", "name": entry, "full_path": full_path, "rel_path": rel_from_source})
    return tree

# Render the sidebar directory tree into HTML elements (using details/summary elements for folders)
def render_navigation(tree, current_file_relative_path: Path, navigation_can_toggle):
    chunks = ['<ul class="ul-style-none">']
    for node in tree:
        if node["type"] == "dir":
            # Check if this folder contains the currently active file
            is_active = current_file_relative_path.is_relative_to(node["rel_path"]) and current_file_relative_path != node["rel_path"]
            open_attr = "open" if (is_active or not navigation_can_toggle) else None
            sub_nav_html = render_navigation(node["children"], current_file_relative_path, navigation_can_toggle)
            summary_elem_html = html_tag("summary", f"🗀 {node['name']}")
            chunks.append(html_tag("li", html_tag("details", f"{summary_elem_html}{sub_nav_html}", {"open": open_attr})))
        elif node["type"] == "file":
            # Create a link for each markdown file
            target_html_path = node["rel_path"].with_suffix('.html').as_posix()
            is_current = (node["rel_path"] == current_file_relative_path)
            link_elem_html = build_link_tag(f"/{target_html_path}", node["name"][:-3], target=None, rel=None, cls="current-file" if is_current else None)
            chunks.append(html_tag("li", link_elem_html))
    chunks.append('</ul>')
    return "".join(chunks)

# Build desktop and mobile sidebars (index navigation and table of contents)
def build_sidebars(config, dir_tree, current_file_relative_path: Path, toc):
    # If navigation is disabled, only return the Table of Contents sidebar
    if not config.get("navigation_enabled", True):
        return "", "", html_tag("div", f'{html_tag("h3", config.get("toc_title", "TOC"), {"class": "sidebar-title"})}{html_tag_void("hr")}{toc}', {"class": "sidebar sidebar-right desktop"})

    can_toggle = config.get("navigation_can_toggle", False)
    navigation_nodes_list = render_navigation(dir_tree, current_file_relative_path, can_toggle).strip()

    # Build desktop and mobile versions of the left sidebar (index) and right sidebar (TOC)
    left_sidebar_html = html_tag("div", f'{html_tag("h3", "Index", {"class": "sidebar-title"})}{html_tag_void("hr")}{html_tag("nav", navigation_nodes_list)}', {"class": "sidebar sidebar-left desktop"})
    left_sidebar_mobile_html = html_tag("div", html_tag("details", f'{html_tag("summary", "Index", {"class": "sidebar-title"})}{html_tag("nav", navigation_nodes_list)}'), {"class": "sidebar sidebar-left mobile"})

    toc_title = config.get("toc_title", "TOC")
    right_sidebar_mobile_html = html_tag("div", html_tag("details", f'{html_tag("summary", toc_title, {"class": "sidebar-title"})}{toc}'), {"class": "sidebar sidebar-right mobile"})
    right_sidebar_desktop_html = html_tag("div", f'{html_tag("h3", toc_title, {"class": "sidebar-title"})}{html_tag_void("hr")}{toc}', {"class": "sidebar sidebar-right desktop"})

    return left_sidebar_html, left_sidebar_mobile_html, right_sidebar_mobile_html, right_sidebar_desktop_html

# Resolve custom friendly page titles and browser window titles from the configuration
def resolve_page_titles(filename, config):
    page_title = filename[:-3]
    page_titles = config.get("page_titles", {})
    if page_title in page_titles:
        page_title = page_titles[page_title]

    # Combine site title and page title for the browser window title
    browser_title = config.get("browser_title", "")
    browser_title = f"{browser_title} | {page_title}" if browser_title else page_title
    return page_title, browser_title

# Build the main site header container with logo/icon, title, and subtitle
def build_header_section(config, header_nav_html):
    header_title = config.get("header_title", "Title")
    icon_path = config.get("header_icon", "")
    icon_html = build_img_tag(icon_path, cls="header-icon", loading=None) if icon_path else ""
    subtitle_path = config.get("header_subtitle", "")
    subtitle_html = html_tag("p", subtitle_path) if subtitle_path else ""

    # Wrap header title and icon in a link pointing to the homepage
    header_link_html = build_link_tag("/", f"{icon_html} {header_title}", target=None, rel=None)
    header_h1_html = html_tag("h1", header_link_html, {"id": "header_title"})
    return html_tag("header", f"{header_h1_html}{subtitle_html}{header_nav_html}")

# Assemble all HTML parts into a final complete HTML5 document string
def assemble_html_document(config, meta, assets, content_html, sidebars):
    browser_title, keywords, css_link_html, custom_css_link_html = meta
    header_container_html, left_mobile_html, right_mobile_html, left_desktop_html, right_desktop_html = sidebars

    main_html = html_tag("main", content_html)

    # Combine everything into a complete HTML document template
    final_html = f"""
    <!DOCTYPE html>
    <html>

    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="keywords" content="{keywords}"><title>{browser_title}</title>
        {css_link_html}
        {custom_css_link_html}
    </head>

    <body>
    {header_container_html}
    {SEARCH_WIDGET_JS_html}
    {left_mobile_html}
    {right_mobile_html}
    {left_desktop_html}
    {main_html}
    {right_desktop_html}
    {CARD_JS_html if ENABLE_CORS_THUMBNAIL_FETCH else ""}
    {GO_UP_BUTTON_JS_html}
    </body>
    </html>"""

    return final_html

# Validate whether a file path falls under valid navigation and folder filters
def is_valid_nav_folder(src_file: Path, source_dir: Path, config):
    nav_folders = [Path(f) for f in config.get("navigation_folders", [])]
    attachments_folders = [Path(f) for f in config.get("attachments_folders", [])]
    return (not nav_folders or any(src_file.is_relative_to(source_dir / f) for f in nav_folders)) and \
           not any(src_file.is_relative_to(source_dir / f) for f in attachments_folders)

# ==============================================================================
# CONVERT SINGLE FILE (MARKDOWN to HTML)
# ==============================================================================

# Process a single file (compile markdown to HTML, copy non-md files, collect search index data)
def process_single_file(file_info, source_dir: Path, target_dir: Path, target_css_filename, custom_target_css_filename, config, dir_tree):
    root, filename = file_info
    src_file = Path(root) / filename
    rel_path = src_file.parent.relative_to(source_dir)
    target_root = target_dir / rel_path

    # If it's not a Markdown file, simply copy it over to the output directory
    if not filename.endswith(".md"):
        shutil.copy2(src_file, target_root / filename)
        return None

    # Parse Markdown file and build sidebars
    content_html, toc, markdown_content = parse_markdown_file(src_file, config)
    current_file_relative_path = src_file.relative_to(source_dir)

    left_sidebar_html, left_sidebar_mobile_html, right_sidebar_mobile_html, right_sidebar_desktop_html = build_sidebars(
        config, dir_tree, current_file_relative_path, toc
    )

    # Calculate relative path to the root directory for CSS stylesheet links
    path_to_root = target_dir.relative_to(target_root, walk_up=True) if hasattr(Path, 'walk_up') else Path(os.path.relpath(target_dir, target_root))

    css_link_html = f'<link rel="stylesheet" href="{(path_to_root / target_css_filename).as_posix()}">' if target_css_filename else ""
    custom_css_link_html = f'<link rel="stylesheet" href="{(path_to_root / custom_target_css_filename).as_posix()}">' if custom_target_css_filename else ""

    page_title, browser_title = resolve_page_titles(filename, config)
    header_nav_html = generate_header_nav(config)
    header_container_html = build_header_section(config, header_nav_html)

    # Assemble the final HTML document string
    final_html = assemble_html_document(
        config=config,
        meta=(browser_title, config.get("keywords", ""), css_link_html, custom_css_link_html),
        assets=None,
        content_html=content_html,
        sidebars=(header_container_html, left_sidebar_mobile_html, right_sidebar_mobile_html, left_sidebar_html, right_sidebar_desktop_html)
    )

    # Write the output HTML file
    with open(target_root / f"{src_file.stem}.html", 'w', encoding='utf-8') as f:
        f.write(final_html)

    log(f"{src_file}: done!")

    # Return search index data entry if the file is part of active navigation folders
    if is_valid_nav_folder(src_file, source_dir, config):
        display_title_path = rel_path / f"{src_file.stem}.md"
        display_title_str = display_title_path.as_posix()

        url_path = rel_path / f"{src_file.stem}.html"
        return {
            "title": page_title,
            "displayTitle": display_title_str,
            "url": "/" + url_path.as_posix(),
            "snippet": clean_snippet(markdown_content)
        }
    return None

# ==============================================================================
# MAIN CONVERSION HELPERS
# ==============================================================================

# Prepare the target output directory, clear previous builds, and copy stylesheets
def prepare_output_directory(target_dir: Path, css_file_path: Path, config):
    # Remove existing output directory and create a fresh one
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy base CSS stylesheet to the output directory
    target_css_filename = css_file_path.name if css_file_path else None
    if css_file_path and css_file_path.exists():
        shutil.copyfile(css_file_path, target_dir / target_css_filename)

    # Copy custom user CSS stylesheet if specified in config
    custom_css_path = config.get("css_file_path")
    custom_target_css_filename = Path(custom_css_path).name if custom_css_path else None
    if custom_css_path and Path(custom_css_path).exists():
        shutil.copyfile(custom_css_path, target_dir / custom_target_css_filename)

    return target_css_filename, custom_target_css_filename

# Copy static attachment folders specified in the config to the output directory
def copy_attachments(source_dir: Path, target_dir: Path, config):
    for src_rel_path, dest_rel_path in config.get("attachments_folders", {}).items():
        full_src_path = source_dir / src_rel_path
        full_dest_path = target_dir / dest_rel_path
        if full_src_path.exists():
            shutil.copytree(full_src_path, full_dest_path, dirs_exist_ok=True)

# Collect all files across directories to process, ignoring hidden files
def collect_files_to_process(source_dir: Path, target_dir: Path):
    files_to_process = []
    for root, dirs, files in os.walk(source_dir):
        # Ignore hidden folders
        dirs[:] = [d for d in dirs if not is_hidden(d)]
        rel_root = Path(root).relative_to(source_dir)
        (target_dir / rel_root).mkdir(parents=True, exist_ok=True)
        for filename in files:
            # Ignore hidden files
            if not is_hidden(filename):
                files_to_process.append((root, filename))
    return files_to_process

# Write out the aggregated search index JSON file for the search widget
def write_search_index(target_dir: Path, search_files):
    search_data = {"files": search_files}
    with open(target_dir / SEARCH_JSON_FILE, 'w', encoding='utf-8') as sf:
        json.dump(search_data, sf, ensure_ascii=False)
        log(f"SEARCH JSON WRITE ({len(search_files)} files indexed)")

# ==============================================================================
# MAIN CONVERSION LOGIC
# ==============================================================================

# Main controller function coordinating the entire site conversion process
def convert_md_to_html(source_dir: Path, target_dir: Path, css_file_path: Path, config):
    target_css_filename, custom_target_css_filename = prepare_output_directory(target_dir, css_file_path, config)
    copy_attachments(source_dir, target_dir, config)

    dir_tree = build_directory_tree(source_dir, source_dir, config)
    files_to_process = collect_files_to_process(source_dir, target_dir)

    # Bind configuration parameters to the worker function for multiprocessing
    worker_func = partial(
        process_single_file,
        source_dir=source_dir,
        target_dir=target_dir,
        target_css_filename=target_css_filename,
        custom_target_css_filename=custom_target_css_filename,
        config=config,
        dir_tree=dir_tree
    )

    search_files = []
    # Execute file conversions in parallel across multiple CPU cores
    with ProcessPoolExecutor(max_workers=None) as executor:
        for res in executor.map(worker_func, files_to_process):
            if res is not None:
                search_files.append(res)

    write_search_index(target_dir, search_files)


if __name__ == "__main__":
    # Validate command line arguments (expects a path to config.json)
    if len(sys.argv) < 2:
        log("Usage: python script.py config.json", error=True)
        sys.exit(1)

    # Load configuration JSON file
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    input_dir = Path(cfg["input_directory"])
    output_dir = Path(cfg["output_directory"])

    # Automatically create an index.md file if enabled and missing
    if cfg.get("autogenerate_index", False):
        index_filepath = input_dir / "index.md"
        if not index_filepath.exists():
            index_filepath.touch()
            log("index.md didn't exist, so it was created.")
        else:
            log("index.md already exists.")

    ENABLE_CORS_THUMBNAIL_FETCH = cfg.get("enable_cors_thumbnail_fetch", False)
    css_path = Path(__file__).parent / "base.css"

    # Build site into a temporary directory first to prevent partial outputs
    temp_target = Path(str(output_dir) + "_temp")
    convert_md_to_html(input_dir, temp_target, css_path, cfg)

    # Safely replace the old output directory with the newly built temporary directory
    if output_dir.exists():
        shutil.rmtree(output_dir)

    try:
        temp_target.rename(output_dir)
        log(f"Output folder: {output_dir}")
    except FileNotFoundError:
        log(f"The source folder does not exist: {temp_target}", error=True)
    except FileExistsError:
        log(f"A folder with the new name already exists: {output_dir}", error=True)
