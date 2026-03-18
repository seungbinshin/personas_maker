import {
  unstable_v2_createSession,
  type SDKSession,
  type SDKSessionOptions,
  type SDKResultMessage,
  type SDKResultSuccess,
} from "@anthropic-ai/claude-agent-sdk";
import type { WorkerState, RunRequest, RunResult } from "./types";

const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes
// Model can be overridden via CLAUDE_MODEL env var, default: claude-sonnet-4-6
const CLAUDE_MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-4-6";
const BASE_DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit"];

function getDisallowedTools(allowFileWrite?: boolean): string[] {
  if (allowFileWrite) return [];
  const extra = (process.env.CLAUDE_DISALLOWED_TOOLS || "")
    .split(",")
    .map((tool) => tool.trim())
    .filter(Boolean);
  return Array.from(new Set([...BASE_DISALLOWED_TOOLS, ...extra]));
}

function sanitizeUserId(userId: string): string {
  return userId.toLowerCase().replace(/[^a-z0-9]/g, "_").slice(0, 64);
}

function findClaudeExecutable(): string {
  const candidates = [
    process.env.CLAUDE_EXECUTABLE,
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "/usr/bin/claude",
  ].filter(Boolean) as string[];

  return candidates.find((p) => {
    try {
      require("fs").accessSync(p);
      return true;
    } catch {
      return false;
    }
  }) || "claude";
}

export class AgentWorker {
  readonly id: string;
  private _state: WorkerState = "initializing";
  private busySince?: number;
  private session: SDKSession | null = null;
  private projectDir: string;

  constructor(id: string, userId?: string) {
    this.id = id;
    if (process.env.NODE_ENV === "production") {
      const slug = userId ? sanitizeUserId(userId) : "_anonymous";
      this.projectDir = `/home/node/users/${slug}`;
    } else {
      this.projectDir = process.cwd();
    }
  }

  get state(): WorkerState {
    return this._state;
  }

  get status() {
    return { id: this.id, state: this._state, busySince: this.busySince };
  }

  async start(): Promise<void> {
    this._state = "initializing";
    await this._createSession();
  }

  private async _createSession(opts?: {
    cwd?: string;
    model?: string;
    allowFileWrite?: boolean;
  }): Promise<void> {
    const env: Record<string, string> = {};
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) env[k] = v;
    }
    env.CLAUDE_CODE_SKIP_BYPASS_PERMISSIONS_WARNING = "1";
    env.DISABLE_INSTALLATION_CHECKS = "1";
    // Prevent nested Claude Code session error
    delete env.CLAUDECODE;

    // Subscription mode: remove ANTHROPIC_API_KEY so the claude binary uses ~/.claude/ auth
    // Set USE_CLAUDE_API_KEY=1 to keep API key mode
    if (!process.env.USE_CLAUDE_API_KEY) {
      delete env.ANTHROPIC_API_KEY;
    }

    const claudeExe = findClaudeExecutable();
    const effectiveCwd = opts?.cwd || this.projectDir;
    const effectiveModel = opts?.model || CLAUDE_MODEL;

    console.log(
      `[Agent] Worker ${this.id}: model=${effectiveModel} exe=${claudeExe} cwd=${effectiveCwd}` +
      (opts?.allowFileWrite ? " fileWrite=ON" : "")
    );

    if (process.env.NODE_ENV === "production") {
      const fs = require("fs");
      const { execSync } = require("child_process");

      fs.mkdirSync(effectiveCwd, { recursive: true });

      if (!fs.existsSync(`${effectiveCwd}/.git`)) {
        try { execSync("git init", { cwd: effectiveCwd, stdio: "ignore" }); } catch {}
      }

      // Anonymous worker: clear previous memory to ensure stateless requests
      if (effectiveCwd.endsWith("/_anonymous")) {
        const projectKey = effectiveCwd.replace(/^\//, "").replace(/\//g, "-");
        const memFile = `/home/node/.claude/projects/${projectKey}/memory/MEMORY.md`;
        try { fs.rmSync(memFile, { force: true }); } catch {}
      }
    }

    // cwd is supported at runtime but not yet declared in SDKSessionOptions (alpha API)
    const sessionOptions: SDKSessionOptions & { cwd?: string } = {
      model: effectiveModel,
      pathToClaudeCodeExecutable: claudeExe,
      permissionMode: "bypassPermissions",
      // Prevent LLM from saving reports/artifacts to the filesystem instead of
      // returning them as output text. All persistence is handled by the
      // Python pipeline (report_store / save_artifact), not by the LLM.
      disallowedTools: getDisallowedTools(opts?.allowFileWrite),
      cwd: effectiveCwd,
      env,
    };
    this.session = unstable_v2_createSession(sessionOptions as SDKSessionOptions);

    try {
      await this._warmup(effectiveCwd);
      this._state = "ready";
      console.log(`[Agent] Worker ${this.id} ready (warm)`);
    } catch (err) {
      this._state = "error";
      console.error(`[Agent] Worker ${this.id} warmup failed:`, err);
      throw err;
    }
  }

  /**
   * Create session without warmup — for one-shot override requests where the
   * actual prompt will be the first message. Avoids the "hi" round-trip that
   * causes "process aborted by user" when multiple workers spawn simultaneously.
   */
  private async _createSessionNoWarmup(opts: {
    cwd?: string;
    model?: string;
    allowFileWrite?: boolean;
  }): Promise<void> {
    const env: Record<string, string> = {};
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) env[k] = v;
    }
    env.CLAUDE_CODE_SKIP_BYPASS_PERMISSIONS_WARNING = "1";
    env.DISABLE_INSTALLATION_CHECKS = "1";
    delete env.CLAUDECODE;

    if (!process.env.USE_CLAUDE_API_KEY) {
      delete env.ANTHROPIC_API_KEY;
    }

    const claudeExe = findClaudeExecutable();
    const effectiveCwd = opts.cwd || this.projectDir;
    const effectiveModel = opts.model || CLAUDE_MODEL;

    console.log(
      `[Agent] Worker ${this.id} (no-warmup): model=${effectiveModel} exe=${claudeExe} cwd=${effectiveCwd}` +
      (opts.allowFileWrite ? " fileWrite=ON" : "")
    );

    const sessionOptions: SDKSessionOptions & { cwd?: string } = {
      model: effectiveModel,
      pathToClaudeCodeExecutable: claudeExe,
      permissionMode: "bypassPermissions",
      disallowedTools: getDisallowedTools(opts.allowFileWrite),
      cwd: effectiveCwd,
      env,
    };
    this.session = unstable_v2_createSession(sessionOptions as SDKSessionOptions);
    this._state = "ready";
    console.log(`[Agent] Worker ${this.id} ready (no-warmup, will send prompt directly)`);
  }

  private async _warmup(cwd?: string): Promise<void> {
    if (!this.session) throw new Error("No session");

    let initMsg = "hi";
    if (process.env.NODE_ENV === "production") {
      try {
        const fs = require("fs");
        const effectiveCwd = cwd || this.projectDir;
        const userMd = `${effectiveCwd}/CLAUDE.md`;
        if (fs.existsSync(userMd)) {
          const content = fs.readFileSync(userMd, "utf-8").trim();
          if (content) {
            initMsg = `[Personal settings]\n${content}\n\nRemember the above. Reply "ok" when ready.`;
          }
        }
      } catch {}
    }

    await this.session.send(initMsg);
    for await (const msg of this.session.stream()) {
      if (msg.type === "result") break;
    }
  }

  async execute(request: RunRequest): Promise<RunResult> {
    const hasOverrides = request.cwd || request.model || request.allowFileWrite;

    // If per-request overrides are provided, create a fresh session (skip warmup — the real prompt is the first message)
    if (hasOverrides) {
      this.session?.close();
      this.session = null;
      await this._createSessionNoWarmup({
        cwd: request.cwd,
        model: request.model,
        allowFileWrite: request.allowFileWrite,
      });
    }

    if (this._state !== "ready") {
      throw new Error(`Worker ${this.id} is not ready (state: ${this._state})`);
    }
    if (!this.session) {
      throw new Error(`Worker ${this.id} has no session`);
    }

    this._state = "busy";
    this.busySince = Date.now();
    const startTime = Date.now();
    const timeoutMs = request.timeoutMs || DEFAULT_TIMEOUT_MS;

    try {
      const result = await Promise.race([
        this._ask(request.prompt),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error("hard timeout")), timeoutMs)
        ),
      ]);

      let output: string;
      if (result.subtype === "success") {
        output = (result as SDKResultSuccess).result;
      } else {
        const errResult = result as SDKResultMessage & { errors?: string[] };
        output = errResult.errors?.join("\n") || "Error";
      }

      return {
        success: !result.is_error,
        output,
        durationMs: Date.now() - startTime,
        timedOut: false,
      };
    } catch (err) {
      const timedOut = (err as Error).message === "hard timeout";
      return {
        success: false,
        output: timedOut ? "" : String(err),
        durationMs: Date.now() - startTime,
        timedOut,
        timeoutType: timedOut ? "hard" : undefined,
      };
    } finally {
      this._state = "ready";
      this.busySince = undefined;
    }
  }

  private async _ask(prompt: string): Promise<SDKResultMessage> {
    if (!this.session) throw new Error("No session");
    await this.session.send(prompt);
    for await (const msg of this.session.stream()) {
      if (msg.type === "result") {
        return msg;
      }
    }
    throw new Error("Session ended without result");
  }

  dispose(): void {
    this._state = "disposed";
    try {
      this.session?.close();
    } catch {}
    this.session = null;
    console.log(`[Agent] Worker ${this.id} disposed`);
  }
}
