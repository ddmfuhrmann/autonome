# autonome task #25: POST /links with URL validation
## What to do
Add POST /links that accepts {"url": ...}, validates it is an absolute http(s) URL and stores a new
short link with a random 6-char code. Invalid URLs return 422.
## Done when
- tests cover a valid URL (201 with code), and invalid URLs (ftp://, relative, empty) returning 422
## Conventions for these layers
### Input validation
Input validation rejects control characters and whitespace in URLs.
### No raw 500
Every expected failure is an HTTPException with a string detail; no path returns a raw 500.
### Response models
API responses use response models, never bare dicts.
