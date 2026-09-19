# Privacy Policy

**Last updated: 2026-09-19**

This policy covers **aws-doc-gemini-sync**, an open-source command-line tool that
copies AWS public documentation into Google Docs in the user's own Google Drive.

The short version: the tool runs entirely on the user's own machine, talks only to
Amazon's public documentation site and to Google's APIs on that user's behalf, and
sends nothing anywhere else. The author operates no server and receives no data.

## Who runs this software

There is no hosted service. Each user installs the tool from
[the source repository](https://github.com/Mr-Kondo/aws-doc-gemini-sync) and runs
it on their own computer, under their own Google account, against a Google Cloud
project they create themselves. The author of the software has no access to any
user's Google account, Drive contents, or credentials, and no ability to obtain
them.

## What the tool accesses

**Google user data**, through OAuth 2.0 with the user's explicit consent:

| Scope | What it permits | Why the tool needs it |
|---|---|---|
| `.../auth/drive.file` | Create and modify only the files this application itself creates | To create and update the Google Docs that hold the synchronized documentation |
| `.../auth/documents.readonly` | Read Google Docs documents | To read a document back after writing it, and confirm the upload did not produce an empty file |

`drive.file` is a per-file scope: it grants no access to any other file in the
user's Drive. The tool never lists, reads, moves, or deletes files it did not
create. It contains no delete operation for documents at all.

**Public data**: the tool downloads pages from `docs.aws.amazon.com`, which are
publicly available and require no credentials.

## What the tool does with that data

AWS documentation pages are downloaded, cleaned up deterministically, and written
into Google Docs in a Drive folder the user nominates. That is the entire purpose,
and the entire use.

The tool does not transmit user data to the author or to any third party. It
performs no analytics, no telemetry, no crash reporting, and no usage tracking.
It contains no such code and no such dependency.

Its only outbound network destinations are:

- `docs.aws.amazon.com` — to download public documentation
- `www.googleapis.com`, `oauth2.googleapis.com`, `accounts.google.com` — Google's
  own APIs and sign-in, acting as the user
- `127.0.0.1` / `localhost` — the loopback address used to complete the OAuth
  consent flow locally

## What is stored, and where

Everything the tool stores is a local file on the user's own computer. Nothing is
stored anywhere the author or any other party can reach.

| File | Contents | Notes |
|---|---|---|
| `.state/token.json` | The OAuth refresh token | Created with owner-only permissions (`0600`). Deleting it revokes the tool's cached access and forces re-authorization. |
| `.state/manifest.json` | URLs, content hashes, Google Doc ids, timestamps | No document text, no credentials |
| `.state/content-cache/` | Copies of the downloaded AWS documentation | Public AWS content only |
| `.env` | The user's own Google client configuration | Never committed; excluded by `.gitignore` |

Log output is written to the user's terminal. Credentials, tokens, and document
bodies are never logged; fields whose names suggest a secret are redacted before
a log line is emitted.

There is no retention period to describe, because there is no server: the user
deletes these files whenever they choose, and the data is gone.

## Sharing

None. No user data is sold, rented, shared, or disclosed to anyone. The author
never receives it in the first place.

## Limited use

This application's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Revoking access

Access can be withdrawn at any time, independently of the tool:

1. Delete the local token file (`.state/token.json`), and/or
2. Remove the application at
   [Google Account → Third-party access](https://myaccount.google.com/connections).

Documents already created remain in the user's Drive and belong to the user. The
tool cannot delete them.

## Children

This is a developer tool and is not directed at children.

## Changes

Changes to this policy are published in this file, and its revision history is
public in the repository.

## Contact

Please open an issue at
[github.com/Mr-Kondo/aws-doc-gemini-sync/issues](https://github.com/Mr-Kondo/aws-doc-gemini-sync/issues).
