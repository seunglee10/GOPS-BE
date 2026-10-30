#!/usr/bin/env node
/*
 * 목업 코스(gops.js COURSE·HINTS·PZ·DAYS + course-data.js COURSE_CHARTS)를
 * API 시드 JSON(D29)으로 바꾼다. 함수형 정답·가격대·해설은 여기서 미리 평가한다.
 *
 * 사용:
 *   node scripts/learn/convert_mock_course.mjs [목업 assets 폴더] [출력 파일]
 * 기본값:
 *   ~/gops-web-mock-learn-split/assets
 *   systems/api-server/pods/api-server/app/learn/data/chart-basics.json
 *
 * gops.js 는 화면 코드가 섞여 있어 통째로 실행하지 않고, 코스 구간
 * ("차트 읽기 코스" 주석 ~ "function qView" 직전)만 잘라 vm 샌드박스에서 돌린다.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const assets = path.resolve(process.argv[2] || path.join(os.homedir(), "gops-web-mock-learn-split", "assets"));
const out = path.resolve(
  process.argv[3] || path.join(repoRoot, "systems/api-server/pods/api-server/app/learn/data/chart-basics.json"),
);

const COURSE_ID = "chart-basics";
const TITLE_KO = "차트 읽기 첫걸음";

const gops = fs.readFileSync(path.join(assets, "gops.js"), "utf8");
const chartsSrc = fs.readFileSync(path.join(assets, "course-data.js"), "utf8");

const start = gops.indexOf("/* ================= 차트 읽기 코스");
const end = gops.indexOf("function qView(");
if (start < 0 || end < 0 || end < start) throw new Error("gops.js 에서 코스 구간을 찾지 못했다");
const courseSrc = gops.slice(start, end);

// 코스 구간이 참조하는 화면 함수는 쓰지 않는다. store 만 빈 껍데기로 둔다.
const sandbox = { store: { get: () => null, set: () => {} }, console };
vm.createContext(sandbox);
const result = vm.runInContext(
  `${chartsSrc}\n${courseSrc}\n;({ COURSE, HINTS, PZ, DAYS, COURSE_CHARTS })`,
  sandbox,
  { filename: "mock-course.js" },
);
const { COURSE, HINTS, PZ, DAYS, COURSE_CHARTS } = result;

const evalMaybe = v => (typeof v === "function" ? v() : v);
const plain = v => JSON.parse(JSON.stringify(v));

const questions = COURSE.map(q => {
  const ans = evalMaybe(q.ans);
  if (q.kind === "choice" && typeof ans !== "number") throw new Error(`${q.id}: choice 정답이 숫자가 아니다`);
  if (q.kind === "tap" && !Array.isArray(ans)) throw new Error(`${q.id}: tap 정답이 배열이 아니다`);
  if (q.kind === "level" && typeof q.band !== "function") throw new Error(`${q.id}: level 에 band 가 없다`);
  return {
    id: q.id,
    day: q.day,
    topic: q.topic,
    title: q.title,
    kind: q.kind,
    prompt: q.prompt,
    opts: q.opts ? [...q.opts] : null,
    answer: typeof ans === "number" ? ans : null,
    answerSet: Array.isArray(ans) ? [...ans] : null,
    band: q.band ? [...q.band()] : null,
    tolerance: q.tol ?? null,
    count: q.count ?? null,
    bridge: q.bridge ?? null,
    fig: plain(q.fig),
    hints: HINTS[q.id] ? [...HINTS[q.id]] : [],
    explanation: String(evalMaybe(q.exp)),
    zone: PZ[q.id] ? [...PZ[q.id]] : null,
  };
});

const usedCharts = new Set(questions.map(q => q.fig.chart).filter(Boolean));
const charts = {};
for (const key of Object.keys(COURSE_CHARTS)) {
  if (!usedCharts.has(key)) continue;
  charts[key] = plain(COURSE_CHARTS[key]);
}
for (const key of usedCharts) if (!charts[key]) throw new Error(`차트 ${key} 가 course-data.js 에 없다`);

const payload = {
  courseId: COURSE_ID,
  titleKo: TITLE_KO,
  days: Object.entries(DAYS).map(([day, titleKo]) => ({ day: Number(day), titleKo })),
  questions,
  charts,
};

fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify(payload, null, 1) + "\n");
console.log(`wrote ${path.relative(process.cwd(), out)}: ${questions.length} questions, ${Object.keys(charts).length} charts`);
