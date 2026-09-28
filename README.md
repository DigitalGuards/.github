# DigitalGuards shared GitHub tooling

## Release announcements for Discord

`actions/release-discord` posts a published GitHub release to a Discord channel as one message: a heading with the project name and tag, the release notes, and a link to the release page for downloads. The message is plain Markdown content, so it can be forwarded to another server or copied as text.

### Setup in a repository

1. Store the channel's Discord webhook URL as the repository secret `DISCORD_RELEASE_WEBHOOK`. A webhook URL that ends in `/github` (the form GitHub repository webhooks use) works as is.
2. Copy `examples/release-announcement.yml` to `.github/workflows/release-announcement.yml`, set `project`, and replace `<commit-sha>` with the full commit SHA of the tag `release-discord-v1` (`git rev-parse release-discord-v1^{commit}`).

The action runs with the webhook secret in its environment, so callers pin it to a commit SHA. A later push to this repository changes nothing until a caller moves its pin.

For a `release` event GitHub reads the workflow from the tagged commit, so the file has to be on the branch releases are tagged from. Releases created with the workflow `GITHUB_TOKEN` trigger no workflows at all; releases cut by hand or with a personal token do.

### Writing the notes

- The whole release body is posted when it has no markers.
- A block between `<!-- discord -->` and `<!-- /discord -->` is posted in place of the body. Use it for a community announcement next to technical notes such as checksums and validation logs.
- Discord caps a message at 2000 characters. The heading and the link take about 150, and longer notes are cut at a paragraph with a pointer to the release page.
- Headings move below the message title, images and HTML are dropped, `#123` and GitHub pull request URLs become links, and code is kept as written.
- Mentions never ping: `@everyone` and user or role mentions are posted as plain text.

### Running it by hand

Run the workflow from the Actions tab with a tag to announce an existing release again. Tick `dry-run` to print the message in the job log instead of posting it. Prereleases are announced from a manual run, and from the release event only with `prereleases: true`.

### Tests

```sh
cd actions/release-discord && python3 -m unittest -v test_announce
```
