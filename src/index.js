/**
 * Вебхук-бот «Остаток по смете» для Cloudflare Workers.
 *
 * Даёт мгновенный (секунды, а не 15 минут как у cron-версии в
 * ../budget_remainder_bot.py) пересчёт budget_remainder сразу как только
 * задача формы попадает во входящие бота — то есть сразу после того, как
 * заявка сохранена. Cloudflare Workers выбран вместо Vercel/Render потому
 * что не "засыпает" между запросами — важно, т.к. Pyrus ждёт ответ на
 * вебхук не дольше 60 секунд.
 *
 * Настройка на стороне Pyrus (только через веб, API этого не поддерживает):
 *   1. pyrus.com/t#bots → создать бота → скопировать сгенерированный Pyrus
 *      secret key (X-Pyrus-Sig подписывается им) и URL этого воркера
 *      (https://<worker>.<subdomain>.workers.dev) в настройки бота.
 *   2. В конструкторе формы 2455896 добавить этого бота участником шага 1
 *      «Заполнение заявки» — ВАЖНО: как наблюдателя/неблокирующего
 *      участника, если в конструкторе такой вариант есть, а не как
 *      обязательного согласующего — иначе сбой вебхука мог бы застопорить
 *      реальную заявку (на нашей стороне это тоже подстраховано — любая
 *      внутренняя ошибка логируется, но воркер всё равно отвечает 200).
 *
 * Секреты (Cloudflare dashboard → Workers & Pages → Settings → Variables,
 * тип "Secret", либо `wrangler secret put <NAME>`):
 *   PYRUS_LOGIN            — pyrus_demo@outlook.com
 *   PYRUS_SECURITY_KEY     — секретный ключ Pyrus API (тот же, что в GitHub Secrets)
 *   PYRUS_WEBHOOK_SECRET   — secret key бота из шага 1 выше (НЕ security_key API)
 *
 * Файл специально самодостаточный (логика расчёта продублирована из
 * budget_remainder_bot.py) — при изменении формулы остатка правьте оба места.
 */

const FORM_ID = 2455896;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    const rawBody = await request.text();
    const signature = request.headers.get("X-Pyrus-Sig") || "";

    if (!(await isSignatureValid(rawBody, env.PYRUS_WEBHOOK_SECRET, signature))) {
      return new Response("invalid signature", { status: 403 });
    }

    let payload;
    try {
      payload = JSON.parse(rawBody);
    } catch (e) {
      return new Response("", { status: 200 });
    }

    const taskId = payload.task_id || (payload.task && payload.task.id);
    if (!taskId) {
      return new Response("", { status: 200 });
    }

    try {
      await recalcBudgetRemainder(taskId, env);
    } catch (e) {
      // Намеренно не роняем вебхук ошибкой — если бот добавлен обязательным
      // участником шага, 4xx/5xx-ответ мог бы застопорить реальную заявку
      // из-за сбоя в нашем расчёте. Ошибку просто логируем.
      console.error(`budget remainder webhook error for task ${taskId}:`, e);
    }

    return new Response("", { status: 200 });
  },
};

async function isSignatureValid(rawBody, secret, signatureHex) {
  if (!secret || !signatureHex) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-1" },
    false,
    ["sign"]
  );
  const sigBuffer = await crypto.subtle.sign("HMAC", key, enc.encode(rawBody));
  const digestHex = [...new Uint8Array(sigBuffer)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return timingSafeEqualHex(digestHex, signatureHex.toLowerCase());
}

function timingSafeEqualHex(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

async function pyrusAuth(env) {
  const resp = await fetch("https://accounts.pyrus.com/api/v4/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ login: env.PYRUS_LOGIN, security_key: env.PYRUS_SECURITY_KEY }),
  });
  if (!resp.ok) throw new Error(`Pyrus auth failed: ${resp.status}`);
  const data = await resp.json();
  return { token: data.access_token, apiUrl: data.api_url };
}

async function pyrusGet(apiUrl, token, path, params) {
  const url = new URL(`${apiUrl}${path}`);
  if (params) {
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  }
  const resp = await fetch(url.toString(), { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) throw new Error(`GET ${path} failed: ${resp.status}`);
  return resp.json();
}

async function pyrusPost(apiUrl, token, path, payload) {
  const resp = await fetch(`${apiUrl}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(`POST ${path} failed: ${resp.status}`);
  return resp.json();
}

function fieldByCode(fields, code) {
  for (const f of fields || []) {
    if (f.code === code || (f.info && f.info.code === code)) return f;
  }
  return null;
}

function objCatQty(fields) {
  const objField = fieldByCode(fields, "object");
  const catField = fieldByCode(fields, "material_category");
  const qtyField = fieldByCode(fields, "quantity");
  if (!objField || !catField || !qtyField) return null;

  const objValue = objField.value && Array.isArray(objField.value.values) ? objField.value.values[0] : null;
  const catValue = catField.value && Array.isArray(catField.value.values) ? catField.value.values[0] : null;
  if (!objValue || !catValue) return null;

  const qty = parseFloat(qtyField.value);
  return [objValue, catValue, Number.isFinite(qty) ? qty : 0];
}

async function getLimit(apiUrl, token, objectName, categoryName) {
  const catalogsResp = await pyrusGet(apiUrl, token, "catalogs");
  const catalog = (catalogsResp.catalogs || []).find(
    (c) => c.name === "Лимиты по объектам" && !c.deleted
  );
  if (!catalog) return null;

  const catalogData = await pyrusGet(apiUrl, token, `catalogs/${catalog.catalog_id}`);
  for (const item of catalogData.items || []) {
    // Порядок values: [Ключ, Категория, Лимит, Период, Объект (полное имя)] —
    // см. докстринг budget_remainder_bot.py про ограничение can_not_modify_first_column
    const values = item.values;
    if (values && values.length >= 5 && values[4] === objectName && values[1] === categoryName) {
      return parseFloat(values[2]);
    }
  }
  return null;
}

async function sumUsedBudget(apiUrl, token, objectName, categoryName, excludeTaskId) {
  const register = await pyrusGet(apiUrl, token, `forms/${FORM_ID}/register`, { include_archived: "n" });
  let total = 0;
  for (const task of register.tasks || []) {
    if (task.id === excludeTaskId) continue;
    const parsed = objCatQty(task.fields || []);
    if (parsed && parsed[0] === objectName && parsed[1] === categoryName) {
      total += parsed[2];
    }
  }
  return total;
}

async function recalcBudgetRemainder(taskId, env) {
  const { token, apiUrl } = await pyrusAuth(env);
  const taskResp = await pyrusGet(apiUrl, token, `tasks/${taskId}`);
  const fields = taskResp.task.fields || [];

  const parsed = objCatQty(fields);
  if (!parsed) return; // заявка ещё не заполнена настолько, чтобы считать остаток
  const [objectName, categoryName, quantity] = parsed;

  const limit = await getLimit(apiUrl, token, objectName, categoryName);
  if (limit === null) return;

  const used = await sumUsedBudget(apiUrl, token, objectName, categoryName, taskId);
  const remainder = limit - used - quantity;

  await pyrusPost(apiUrl, token, `tasks/${taskId}/comments`, {
    field_updates: [{ code: "budget_remainder", value: remainder }],
  });
}
