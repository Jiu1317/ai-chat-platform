# ChatGPT-Web2API MCP Server Guide

## Overview

This MCP server exposes ChatGPT's web interface as a set of tools, resources, and prompts for AI agents. It connects to an authenticated Chrome browser session running ChatGPT and provides:

- **Chat completions** with multi-turn conversation support
- **Project-scoped memory** for persistent context across sessions
- **Conversation management** — list, retrieve, rename, archive, delete
- **Project management** — list, create, update instructions
- **Model catalog** — discover available models and their capabilities

## Prerequisites

1. Run `chatgpt-web2api` to start Chrome with an authenticated ChatGPT session
2. The MCP server connects to the same Chrome instance via CDP (default port 9222)

## Mental Model: ChatGPT Context Levels

ChatGPT has 3 levels of context persistence:

```
Level 1: Single Message (ephemeral)
  → message sent, response received, no memory

Level 2: Multi-turn Conversation (session-scoped)
  → conversation_id tracks a thread of messages
  → ChatGPT remembers earlier messages within the same conversation
  → Dies when conversation is deleted or forgotten

Level 3: Project (persistent across sessions)
  → project_id scopes all conversations to a workspace
  → Has its own memory (facts ChatGPT remembers between conversations)
  → Has custom instructions (system prompt for ALL chats in the project)
  → Has uploaded files (knowledge base for the project)
```

## When to Use What

| Goal | Tool | Key Parameters |
|------|------|---------------|
| Quick one-off question | `chat_completion` | `message` only |
| Start an independent chat | `chat_completion` | `message` only |
| Continue previous chat | `chat_completion` | `message` + `conversation_id` |
| Start fresh with system prompt | `chat_completion` | `message` + `system_prompt` |
| Use persistent memory | `chat_completion` | `message` + `project_id` |
| Create isolated workspace | `create_project` | `name` + `memory_scope` |
| List recent chats | `list_conversations` | `limit` (default 28) |
| Find a specific chat | `list_conversations` | `search` query |
| Clean up old chats | `delete_conversation` | `conversation_id` |

## Projects and Memory

Projects are the key differentiator. Each project is an isolated workspace with:

- **Custom instructions**: A system prompt that applies to all conversations in the project
- **Memory**: Facts that ChatGPT remembers across conversations in the project
- **Files**: Uploaded documents that ChatGPT can reference

### Memory Scopes

| Scope | Behavior | Use When |
|-------|----------|----------|
| `project_v2` | Dedicated memory — only this project's conversations contribute | Isolated task, specific domain, no cross-contamination |
| `global` | Shared memory — uses the global ChatGPT memory pool | General-purpose, cross-project context needed |

### Creating a Project Without Shared Memory

To create an isolated project with its own dedicated memory:

```
create_project(
    name="My Isolated Project",
    instructions="You are a specialist in...",
    memory_scope="project_v2"
)
```

This creates a project where ChatGPT's memory is scoped only to conversations within that project — no leakage from other chats.

### Using a Project

After creating a project, pass its `project_id` to `chat_completion`:

```
chat_completion(
    message="What do you remember about our previous discussion?",
    project_id="g-p-abc123..."
)
```

## System Prompts

System prompts are prepended to the user message as `[System Instructions]\n...\n\n[User]\n...`. This works reliably for most cases but note:

- Omitting `conversation_id` always starts a **new isolated conversation**
- For persistent system prompts, use **project instructions** instead
- Project instructions are set via `update_project_instructions` and persist across all conversations

## Conversation Flow

### Explicit Continuation Behavior

The `chat_completion` tool starts a fresh isolated conversation whenever
`conversation_id` is omitted. To send a follow-up, retain the
`conversation_id` returned by the previous response and pass it explicitly on
the next call. This prevents independent clients or workers from inheriting a
different request's browser state.

### Explicit Conversation Control

To start a new conversation explicitly:
- Omit `conversation_id`

To resume a specific conversation:
- Pass `conversation_id` from a previous response

## Model Selection

| Slug | Capabilities | Best For |
|------|-------------|----------|
| `auto` | Auto-selects best model | General use (default) |
| `gpt-5-5` | Latest, reasoning, 34K context | Complex reasoning, analysis |
| `gpt-5-3` | Reasoning, 34K context | Balanced performance |
| `gpt-5-mini` | Fast, no reasoning, 8K context | Simple tasks, speed-critical |
| `gpt-5-3-mini` | Fast, no reasoning, 34K context | Simple tasks with long context |

Use `list_models` to get the current full catalog.

## Resources

The server exposes these MCP resources for live state discovery:

- `chatgpt://models` — Available models (GET, read-only)
- `chatgpt://account` — Current account info and connection status
- `chatgpt://projects/{project_id}` — Specific project details (dynamic URI template)

## Error Handling

- If Chrome is not running: Tools return a connection error
- If not logged in: Connection fails on startup
- If model is unavailable: ChatGPT falls back to default
- If conversation_id is invalid: Returns an error, starts new conversation

## Performance Characteristics

- Simple questions: 7–13 seconds end-to-end
- Complex reasoning: 15–36 seconds
- Follow-up messages (same conversation): 3–6 seconds faster
- Streaming: Available via HTTP API only (MCP returns complete responses)

## Limitations

- **REST image support**: OpenAI image parts are uploaded through Chrome; MCP remains text-only
- **No file upload to projects**: Not yet implemented
- **Cookie expiry**: Authentication cookies expire ~2 weeks; re-login needed
- **Single browser session**: One Chrome profile = one ChatGPT account
- **Rate limits**: Subject to ChatGPT Plus rate limits (~40 messages per 3 hours for GPT-5.5)
