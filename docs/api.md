# PatchScope API

Base URL: `http://127.0.0.1:8787`

Interactive OpenAPI documentation is served at `/docs`. All failures use a sanitized body with `code`, `message`, `request_id`, and bounded `detail`.

## Paste a source file

```http
POST /api/v1/reviews/text
Content-Type: application/json

{
  "name": "Checkout hardening",
  "filename": "checkout.py",
  "content": "def total(value):\n    return eval(value)\n"
}
```

Returns `201` with a complete `ReviewDetail`. The same source identity returns the existing completed review.

## Upload a file or ZIP

```http
POST /api/v1/reviews/file
Content-Type: multipart/form-data

file=@src.zip
name=Checkout hardening
```

The server stops reading after the configured request limit. ZIP contents receive path, symlink, file-count, expanded-byte, compression-ratio, and sensitive-file checks.

## Review a public pull request

```http
POST /api/v1/reviews/github
Content-Type: application/json

{
  "url": "https://github.com/acme/widgets/pull/42",
  "name": "PR #42"
}
```

Only canonical public GitHub pull-request URLs are supported in `0.1.x`. Public requests work
without credentials; an optional server-side `PATCHSCOPE_GITHUB_TOKEN` raises GitHub's public API
rate limit and is never sent to the browser. Private pull requests are not part of the supported or
validated `0.1.x` contract.

## List and read reviews

```http
GET /api/v1/reviews?limit=50&offset=0&status=completed
GET /api/v1/reviews/rev_0123456789abcdef01234567
```

The page response contains `items`, `total`, `limit`, `offset`, and `has_more`.

## Triage a finding

```http
PATCH /api/v1/reviews/{review_id}/findings/{fingerprint}
Content-Type: application/json

{
  "status": "acknowledged",
  "note": "Owner confirmed; remediation is queued."
}
```

Accepted aliases are `accepted -> acknowledged`, `resolved -> fixed`, and `dismissed -> ignored`.

## Export evidence

```http
GET /api/v1/reviews/{review_id}/exports/markdown
GET /api/v1/reviews/{review_id}/exports/sarif
```

Markdown is suitable for a PR description or handoff. SARIF 2.1.0 can be consumed by compatible code-scanning tools.

## Limits

Defaults are 500,000 bytes per file, 2,000,000 bytes per review, 100 files, 20 seconds per analyzer, and 10 seconds per GitHub request. Configure them with `PATCHSCOPE_*` settings.
