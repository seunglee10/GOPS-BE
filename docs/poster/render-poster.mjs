import playwright from "../../apps/gops-frontend/node_modules/@playwright/test/index.js";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const { chromium } = playwright;

const here = path.dirname(fileURLToPath(import.meta.url));
const stem = process.argv[2] || "gops-capstone-a1";
const htmlPath = path.join(here, `${stem}.html`);
const pngPath = path.join(here, `${stem}-preview.png`);
const pdfPath = path.join(here, `${stem}-print.pdf`);

const browser = await chromium.launch({
  headless: true,
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  args: ["--font-render-hinting=none"],
});

try {
  const page = await browser.newPage({
    viewport: { width: 2245, height: 3179 },
    deviceScaleFactor: 1,
  });
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      [...document.images].map((image) => image.complete
        ? Promise.resolve()
        : new Promise((resolve) => {
            image.addEventListener("load", resolve, { once: true });
            image.addEventListener("error", resolve, { once: true });
          }))
    );
  });
  await page.screenshot({ path: pngPath, fullPage: true });
  await page.pdf({
    path: pdfPath,
    width: "594mm",
    height: "841mm",
    margin: { top: "0", right: "0", bottom: "0", left: "0" },
    printBackground: true,
    preferCSSPageSize: true,
  });
  console.log(pngPath);
  console.log(pdfPath);
} finally {
  await browser.close();
}
