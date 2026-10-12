import re

SEARCH_STOPWORDS = {
    "the", "a", "an", "all", "and", "or", "for", "with", "doc", "pdf", "file", "files", 
    "to", "in", "on", "at", "by", "from", "some", "my", "me", "your", "our", "please", 
    "can", "could", "would", "should", "want", "like", "any", "this", "that", "these", 
    "those", "how", "what", "which", "where", "tell", "give", "list", "listing", "show", 
    "get", "find", "search", "locate", "check", "view", "book", "books", "document", "documents", 
    "paper", "copy", "copies", "download", "downloads", "inside", "here", "there", "of", 
    "is", "are", "have", "has", "do", "does", "did", "also", "you", "language", "guide"
}

def clean_q(lower_q):
    cleaned = lower_q.strip()
    # Strip trailing punctuation first
    cleaned = re.sub(r"[?!.,\"';:]+$", "", cleaned).strip()
    # Strip conversational prefixes
    cleaned = re.sub(
        r"^(?:also\s+)?(?:can\s+you\s+)?(?:could\s+you\s+)?(?:please\s+)?(?:find(?: me)?|where is|where are|search(?: for)?|locate|show(?: me)?|get(?: me)?|list|check|do\s+i\s+have)\s+",
        "",
        cleaned
    ).strip()
    # Strip book/doc wrappers
    cleaned = re.sub(r"^(?:the\s+book\s+for|a\s+book\s+for|the\s+book|a\s+book|the\s+guide\s+for|a\s+guide\s+for|the\s+doc\s+for|the\s+file\s+for|a\s+copy\s+of|my|the)\s+", "", cleaned).strip()
    # Strip location trailer (e.g. 'in downloads', 'in the download')
    cleaned = re.sub(r"\s+(?:in|inside|from|of)\s+(?:the\s+)?(?:downloads?|documents?)$", "", cleaned).strip()
    tokens = [t for t in re.findall(r"\w+", cleaned) if len(t) > 1 and t not in SEARCH_STOPWORDS]
    return cleaned, tokens

q = "Also can you find me a book for Java Language in the downloads?"
c, t = clean_q(q.lower())
print("Cleaned:", c)
print("Tokens:", t)

fn = "JavaTheCompleteReference.pdf".lower()
matched = [tok for tok in t if tok in fn]
print("Matched in filename:", matched)
