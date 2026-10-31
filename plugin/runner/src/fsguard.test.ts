import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { judge, scopedFileCommands } from "./fsguard.js";

const wt = mkdtempSync(join(tmpdir(), "wt-"));
mkdirSync(join(wt, "build"));
writeFileSync(join(wt, ".git"), "gitdir: elsewhere\n");
writeFileSync(join(wt, "run.sh"), "");
symlinkSync(tmpdir(), join(wt, "out"));

test("file commands inside the worktree run", () => {
  for (const c of ["rm -rf build", "rm build/*.log", "mkdir -p src/a/b", "chmod +x run.sh", "chmod 755 run.sh",
    "mv run.sh scripts/run.sh", "touch 'new file.txt'", `rm ${join(wt, "run.sh")}`]) {
    assert.equal(judge(c, wt), null, c);
  }
});

test("paths outside the worktree, git's data and chained commands are denied", () => {
  const denied: [string, RegExp][] = [
    ["rm -rf ../other", /outside the worktree/],
    ["rm -rf /tmp/x", /outside the worktree/],
    ["rm ~/.zshrc", /outside the worktree/],
    ["rm -rf out/anything", /outside the worktree/], // a symlink pointing out
    ["rm -rf .", /not the worktree itself/],
    ["rm .git", /git's own data/],
    ["rm build/a && rm -rf /", /one command at a time/],
    ["rm $HOME/x", /one command at a time/],
    ["cp run.sh > /etc/x", /one command at a time/],
    ["rm -rf", /no path given/],
  ];
  for (const [c, why] of denied) assert.match(judge(c, wt) ?? "", why, c);
});

test("the hook allows, denies, and leaves other commands to the permission rules", async () => {
  const hook = scopedFileCommands(wt).hooks[0];
  const ask = (command: string) => hook({ tool_input: { command } } as never, undefined, { signal: new AbortController().signal });
  assert.deepEqual(await ask("./gradlew build"), {});
  assert.equal(((await ask("rm -rf build")) as any).hookSpecificOutput.permissionDecision, "allow");
  assert.equal(((await ask("rm -rf ..")) as any).hookSpecificOutput.permissionDecision, "deny");
});
