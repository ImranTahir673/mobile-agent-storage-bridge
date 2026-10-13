import re

def is_format_sorting_query(text: str) -> bool:
    if not text:
        return False
    t = text.lower().replace('_', ' ').replace('-', ' ')
    direct_patterns = [
        r'same\s+format',
        r'same\s+type',
        r'same\s+extension',
        r'file\s*types?\s+together',
        r'by\s+(?:file\s*)?(?:format|type|extension)',
        r'(?:sort|group|arrange|organize|separate|put|consolidate)\s+(?:all\s+)?(?:the\s+)?(?:by\s+)?(?:file\s*)?(?:format|type|extension)s?',
        r'(?:format|type)\s+subfolders?',
        r'(?:format|type)\s+folders?',
        r'into\s+(?:the\s+)?folders?\s+inside\s+down?loads?',
        r'(?:arrange|sort|group|put)\s+.*(?:same|format|type).*(?:folder|place|download)',
        r'(?:same\s+format\s+files|files\s+of\s+same\s+format)',
        r'(?:all\s+format|each\s+format)\s+in\s+(?:1|one)\s+place'
    ]
    for pattern in direct_patterns:
        if re.search(pattern, t):
            return True
    has_format_concept = any(w in t for w in (
        'format', 'formats', 'filetype', 'file type', 'file types', 'extension', 'extensions', 'same type'
    ))
    has_grouping_concept = any(w in t for w in (
        'sort', 'group', 'arrange', 'organize', 'consolidate', 'separate', 'together', '1 place', 'one place', 'into folders', 'in folders'
    ))
    return bool(has_format_concept and has_grouping_concept)

test_prompts = [
    ('User prompt', 'Lets arrange all the same format files in 1 place . From the downloads. And put them inside the folders inside Dowlands.'),
    ('Quick chip', 'Sort all the file types together in the downloads'),
    ('Chip 2', 'group by format'),
    ('Voucher', 'Sort student fee vouchers'),
    ('Duplicates', 'Clean duplicate downloads'),
    ('Query', 'Where is my java book?'),
    ('Action phrase', 'put same format files in their folders')
]

for label, p in test_prompts:
    res = is_format_sorting_query(p)
    print(f'{label}: {res}')
