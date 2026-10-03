#!/usr/bin/env node
// Runs website/scripts/extract-skills.py and generate-llms-txt.py before
// docusaurus build/start so that:
//   - website/static/api/skills.json (lazy-fetched by src/pages/skills/index.tsx)
//   - website/static/api/skills-meta.json (sidecar metadata for the Skills Hub)
//   - website/static/llms.txt (agent-friendly short docs index)
//   - website/static/llms-full.txt (full docs concat for LLM context)
// all exist without contributors remembering to run Python scripts manually.
// CI workflows still run the extraction explicitly, which is a no-op duplicate
// but matches their historical behaviour.
//
// We also try to pull a fresh copy of skills-index.json (the unified
// multi-source catalog) from the live docs site if it's not already on disk.
// That way local `npm run build` doesn't have to wait on
// scripts/build_skills_index.py crawling every skill source — which takes
// several minutes and burns GitHub API quota — but still gets the same
// 2000+ external skills the deployed site has.
//
// If python3 or its deps (pyyaml) aren't available on the local machine, we
// fall back to writing an empty skills.json so `npm run build` still
// succeeds — the Skills Hub page just shows an empty state, and llms.txt
// generation is skipped. CI always has the deps installed, so production
// deploys get real data.

import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync, statSync, renameSync, unlinkSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const websiteDir = resolve(scriptDir, "..");
const extractScript = join(scriptDir, "extract-skills.py");
const llmsScript = join(scriptDir, "generate-llms-txt.py");
const cronBlueprintsScript = join(scriptDir, "extract-automation-blueprints.py");
const outputFile = join(websiteDir, "static", "api", "skills.json");
const unifiedIndexFile = join(websiteDir, "static", "api", "skills-index.json");
const UNIFIED_INDEX_URL =
  "https://youtab-agent-runtime.youtab.io/docs/api/skills-index.json";
const UNIFIED_INDEX_MAX_AGE_MS = 24 * 60 * 60 * 1000; // 24h
const UNIFIED_INDEX_MAX_BYTES = 64 * 1024 * 1024;
const SAFE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/;

function validSkill(skill) {
  return skill && typeof skill === "object" &&
      typeof skill.name === "string" && skill.name.length > 0 &&
      typeof skill.source === "string" && skill.source.length > 0 &&
      typeof skill.identifier === "string" && skill.identifier.length <= 2048 &&
      SAFE_IDENTIFIER.exec(skill.identifier)?.[0] === skill.identifier &&
      !skill.identifier.split("/").includes("..") &&
      (skill.description === undefined || typeof skill.description === "string") &&
      (skill.tags === undefined || Array.isArray(skill.tags) && skill.tags.every((tag) => typeof tag === "string")) &&
      (skill.repo === undefined || typeof skill.repo === "string") &&
      (skill.extra === undefined || skill.extra !== null && typeof skill.extra === "object" && !Array.isArray(skill.extra));
}

async function boundedResponseText(resp) {
  const advertised = Number(resp.headers.get("content-length"));
  if (advertised > UNIFIED_INDEX_MAX_BYTES) throw new Error("skills index exceeds size limit");
  if (!resp.body) throw new Error("skills index response has no body");
  const chunks = [];
  let size = 0;
  for await (const chunk of resp.body) {
    size += chunk.byteLength;
    if (size > UNIFIED_INDEX_MAX_BYTES) throw new Error("skills index exceeds size limit");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, size).toString("utf8");
}

function writeEmptyFallback(reason) {
  mkdirSync(dirname(outputFile), { recursive: true });
  writeFileSync(outputFile, "[]\n");
  console.warn(
    `[prebuild] extract-skills.py skipped (${reason}); wrote empty skills.json. ` +
      `Install python3 + pyyaml locally for a populated Skills Hub page.`,
  );
}

function runPython(script, label) {
  const r = spawnSync("python3", [script], { stdio: "inherit", cwd: websiteDir });
  if (r.error && r.error.code === "ENOENT") {
    console.warn(`[prebuild] ${label} skipped (python3 not found)`);
    return false;
  }
  if (r.status !== 0) {
    console.warn(`[prebuild] ${label} exited with status ${r.status}`);
    return false;
  }
  return true;
}

async function ensureUnifiedIndex() {
  // If we have a recent copy on disk, trust it.
  try {
    const age = Date.now() - statSync(unifiedIndexFile).mtimeMs;
    if (age < UNIFIED_INDEX_MAX_AGE_MS) {
      return true;
    }
    console.log(
      `[prebuild] skills-index.json is ${(age / 3600000).toFixed(1)}h old; ` +
        `refreshing from ${UNIFIED_INDEX_URL}`,
    );
  } catch {
    // Missing or unreadable cache: fall through to re-fetch.
  }

  try {
    const resp = await fetch(UNIFIED_INDEX_URL, {
      headers: { accept: "application/json" },
      redirect: "manual",
      signal: AbortSignal.timeout(15_000),
    });
    // The catalog is a build input; do not let its origin change on redirect.
    if (new URL(resp.url).origin !== new URL(UNIFIED_INDEX_URL).origin) {
      throw new Error("skills index response changed origin");
    }
    if (!resp.ok) {
      console.warn(
        `[prebuild] skills-index.json fetch returned HTTP ${resp.status}; ` +
          `using local copy if any`,
      );
      return existsSync(unifiedIndexFile);
    }
    let text = await boundedResponseText(resp);
    // Validate every publishable command operand before replacing the cache.
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.skills)) {
        console.warn(
          "[prebuild] skills-index.json from live site has no skills array; ignoring",
        );
        return existsSync(unifiedIndexFile);
      }
      const validSkills = parsed.skills.filter(validSkill);
      if (validSkills.length === 0) {
        console.warn("[prebuild] skills-index.json has no valid skills; ignoring");
        return existsSync(unifiedIndexFile);
      }
      if (validSkills.length !== parsed.skills.length) {
        console.warn(`[prebuild] discarded ${parsed.skills.length - validSkills.length} invalid skills-index rows`);
        parsed.skills = validSkills;
        parsed.skill_count = validSkills.length;
        text = JSON.stringify(parsed);
      }
    } catch (e) {
      console.warn(`[prebuild] skills-index.json from live site is not valid JSON: ${e}`);
      return existsSync(unifiedIndexFile);
    }
    mkdirSync(dirname(unifiedIndexFile), { recursive: true });
    const stagedPath = `${unifiedIndexFile}.youtab-${process.pid}-${randomUUID()}.tmp`;
    try {
      writeFileSync(stagedPath, text, { flag: "wx" });
      renameSync(stagedPath, unifiedIndexFile);
    } finally {
      try { unlinkSync(stagedPath); } catch { /* renamed or never created */ }
    }
    console.log(
      `[prebuild] downloaded skills-index.json from ${UNIFIED_INDEX_URL} ` +
        `(${(text.length / 1024).toFixed(0)} KB)`,
    );
    return true;
  } catch (e) {
    console.warn(`[prebuild] skills-index.json fetch failed: ${e}`);
    return existsSync(unifiedIndexFile);
  }
}

// 0) Pull unified index if we don't have a fresh one.
await ensureUnifiedIndex();

// 1) skills.json — required for the Skills Hub page.
const r = spawnSync("python3", [extractScript], {
  stdio: "inherit",
  cwd: websiteDir,
});
if (r.error && r.error.code === "ENOENT") {
  writeEmptyFallback("python3 not found");
} else if (r.status !== 0) {
  writeEmptyFallback(`extract-skills.py exited with status ${r.status}`);
}

// 2) llms.txt + llms-full.txt — agent-friendly docs entrypoints. Non-fatal.
runPython(llmsScript, "generate-llms-txt.py");

// 3) automation-blueprints-index.json — Automation Blueprints catalog page. Non-fatal; the page
//    renders an empty state if the generator can't run.
runPython(cronBlueprintsScript, "extract-automation-blueprints.py");
