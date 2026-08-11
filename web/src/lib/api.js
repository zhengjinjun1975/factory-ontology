// api.js — 前端 API 封装
export async function setupOntology(csvName, csvContent) {
  const resp = await fetch('/api/ontology/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ csvName, csvContent }),
  });
  return resp.json();
}

export async function setupOntologyMulti(files) {
  const resp = await fetch('/api/ontology/setup-multi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ files }),
  });
  return resp.json();
}

export async function dbSetup(cfg) {
  const resp = await fetch('/api/ontology/db-setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  return resp.json();
}

export async function askOntology(question) {
  const resp = await fetch('/api/ontology/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  });
  return resp.json();
}

export async function fetchStats() {
  const resp = await fetch('/api/ontology/stats');
  return resp.json();
}

export async function fetchLine(lineId) {
  const resp = await fetch(`/api/ontology/line/${encodeURIComponent(lineId)}`);
  return resp.json();
}

export async function fetchSchema() {
  const resp = await fetch('/api/ontology/schema', { cache: 'no-store' });
  return resp.json();
}

export async function analyzeOntology(question) {
  const resp = await fetch('/api/ontology/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  });
  return resp.json();
}

export async function getModel() {
  const resp = await fetch('/api/ontology/model');
  return resp.json();
}

export async function setModel(key) {
  const resp = await fetch('/api/ontology/model', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  });
  return resp.json();
}

export async function fetchVersion() {
  const resp = await fetch('/api/ontology/version');
  return resp.json();
}

export async function fetchExamples() {
  const resp = await fetch('/api/ontology/examples');
  return resp.json();
}

export async function fetchExample(path) {
  const resp = await fetch(`/api/ontology/example?path=${encodeURIComponent(path)}`);
  return resp.json();
}
