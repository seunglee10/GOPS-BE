import { spawn } from "node:child_process";

const commands = [
  [".venv/bin/uvicorn", ["mock_backend.app.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload"], "chart"],
  [".venv/bin/uvicorn", ["agent_backend.app.main:app", "--host", "127.0.0.1", "--port", "8010", "--reload"], "agent"],
  ["npm", ["run", "dev", "--workspace", "frontend"], "front"]
];

const children = commands.map(([command, args, label]) => {
  const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.on("data", (chunk) => process.stdout.write(`[${label}] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[${label}] ${chunk}`));
  return child;
});

const stop = () => {
  children.forEach((child) => child.kill("SIGTERM"));
};

process.on("SIGINT", stop);
process.on("SIGTERM", stop);
