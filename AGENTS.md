# Commit messages

For commits made with AI assistance, include a `Co-authored-by` trailer identifying
the tool that actually assisted with the change. For Codex, use:

```text
Co-authored-by: Codex <noreply@openai.com>
```

For opencode, include the tool name and the active model id:

```text
Co-authored-by: opencode (deepseek-v4.1-flash) <noreply@opencode.ai>
```

Other tools must use their own attribution rather than the Codex identity. Do not
invent an attribution email; use the tool's documented identity or an identity
agreed with the user. Keep the human author identity unchanged.

# GitHub posts

Every GitHub post the assistant writes — pull request bodies and comments — must
open with an attribution line, for example:

```text
🤖 Generated with opencode (deepseek-v4.1-flash) <noreply@opencode.ai>
```
