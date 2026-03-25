const fs = require("fs");
const os = require("os");
const path = require("path");
require("dotenv").config();
const axios = require("axios");
const FormData = require("form-data");
const qrcode = require("qrcode");
const { Client, LocalAuth } = require("whatsapp-web.js");

const API_BASE_URL = process.env.API_BASE_URL || "http://127.0.0.1:8000";
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || "llama3.2:3b";
const ALLOWED_GROUP_IDS = (process.env.ALLOWED_GROUP_IDS || "")
  .split(",")
  .map((id) => id.trim())
  .filter(Boolean);
const DEBUG_LOGS = (process.env.DEBUG_LOGS || "true").toLowerCase() === "true";
const processedMessageIds = new Set();

function logDebug(...args) {
  if (!DEBUG_LOGS) return;
  const timestamp = new Date().toISOString();
  console.log(`[${timestamp}]`, ...args);
}

function normalizePhone(from) {
  return (from || "").split("@")[0].replace(/\D/g, "");
}

function resolveChatId(message) {
  // Em mensagens enviadas por mim (message_create), o grupo pode estar em `to`.
  if (message.fromMe && message.to && message.to.endsWith("@g.us")) {
    return message.to;
  }
  return message.from || "";
}

function extFromMime(mimeType) {
  if (!mimeType) return ".ogg";
  if (mimeType.includes("ogg")) return ".ogg";
  if (mimeType.includes("mpeg")) return ".mp3";
  if (mimeType.includes("wav")) return ".wav";
  if (mimeType.includes("webm")) return ".webm";
  if (mimeType.includes("mp4")) return ".m4a";
  return ".ogg";
}

function formatAgentResponse(payload) {
  const result = payload?.agent_result || payload;
  if (!result) return "Nao consegui processar sua solicitacao.";

  if (result.ok && result.action === "get_report" && result.data) {
    const data = result.data;
    return [
      `Relatorio (${data.period}) [${data.kind_filter}]`,
      `Receitas: R$ ${Number(data.total_income).toFixed(2)}`,
      `Despesas: R$ ${Number(data.total_expense).toFixed(2)}`,
      `Saldo: R$ ${Number(data.balance).toFixed(2)}`,
      `Lancamentos: ${Array.isArray(data.transactions) ? data.transactions.length : 0}`,
    ].join("\n");
  }

  if (result.ok && result.data?.id) {
    return `${result.message}\nID: ${result.data.id}\nTipo: ${result.data.kind}\nCategoria: ${result.data.category}\nValor: R$ ${Number(result.data.amount).toFixed(2)}`;
  }

  return result.message || "Operacao concluida.";
}

async function sendAudioToAgent(media, phone) {
  logDebug("Enviando audio para API", {
    phone,
    mime: media.mimetype,
    base64Size: media.data?.length || 0,
  });
  const extension = extFromMime(media.mimetype);
  const tempFile = path.join(os.tmpdir(), `wa-audio-${Date.now()}${extension}`);
  fs.writeFileSync(tempFile, Buffer.from(media.data, "base64"));

  const formData = new FormData();
  formData.append("audio", fs.createReadStream(tempFile));
  formData.append("phone", phone);
  formData.append("confirm", "false");

  try {
    const response = await axios.post(`${API_BASE_URL}/api/transcribe-and-agent`, formData, {
      headers: formData.getHeaders(),
      maxContentLength: Infinity,
      maxBodyLength: Infinity,
    });
    logDebug("Resposta da API recebida", {
      status: response.status,
      ok: response.data?.agent_result?.ok,
      action: response.data?.agent_result?.action,
    });
    return response.data;
  } finally {
    fs.unlink(tempFile, () => {});
  }
}

const client = new Client({
  authStrategy: new LocalAuth({ clientId: "finance-ia-bot" }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  const outputPath = path.join(process.cwd(), "whatsapp-qr.png");
  qrcode
    .toFile(outputPath, qr, { width: 420, margin: 2 })
    .then(() => {
      console.log("QR code gerado em:", outputPath);
      console.log("Abra a imagem e escaneie com o WhatsApp.");
    })
    .catch((err) => {
      console.error("Falha ao gerar QR PNG:", err.message);
    });
});

client.on("ready", () => {
  console.log("Bot WhatsApp conectado e pronto.");
  console.log("Grupos permitidos:", ALLOWED_GROUP_IDS.length ? ALLOWED_GROUP_IDS.join(", ") : "(nenhum)");
  logDebug("Config bot", {
    API_BASE_URL,
    OLLAMA_MODEL,
    DEBUG_LOGS,
    allowedGroupsCount: ALLOWED_GROUP_IDS.length,
  });

  // Diagnostico para confirmar o ID real dos grupos visiveis nessa sessao.
  client
    .getChats()
    .then((chats) => {
      const groups = chats.filter((chat) => chat.isGroup);
      logDebug(`Grupos visiveis: ${groups.length}`);
      groups.slice(0, 30).forEach((group) => {
        const isAllowed = ALLOWED_GROUP_IDS.includes(group.id._serialized);
        logDebug("Grupo", {
          name: group.name,
          id: group.id._serialized,
          allowed: isAllowed,
        });
      });
      if (ALLOWED_GROUP_IDS.length > 0 && !groups.some((g) => ALLOWED_GROUP_IDS.includes(g.id._serialized))) {
        logDebug("ALERTA: nenhum grupo visivel bate com ALLOWED_GROUP_IDS.");
      }
    })
    .catch((err) => {
      logDebug("Falha ao listar grupos no ready", { message: err.message });
    });
});

async function handleIncomingMessage(message, sourceEvent) {
  const messageId = message.id?._serialized;
  if (messageId && processedMessageIds.has(messageId)) {
    return;
  }
  if (messageId) {
    processedMessageIds.add(messageId);
    if (processedMessageIds.size > 5000) {
      processedMessageIds.clear();
    }
  }

  const chatId = resolveChatId(message);
  if (!chatId.endsWith("@g.us")) return;
  if (ALLOWED_GROUP_IDS.length > 0 && !ALLOWED_GROUP_IDS.includes(chatId)) return;

  logDebug("Mensagem recebida (grupo permitido)", {
    event: sourceEvent,
    id: message.id?._serialized,
    from: message.from,
    to: message.to,
    chatId,
    author: message.author,
    type: message.type,
    fromMe: message.fromMe,
    hasMedia: message.hasMedia,
  });

  const phone = normalizePhone(message.author || message.from || message.to);
  logDebug("Processando mensagem do grupo permitido", { group: chatId, phone });

  try {
    const isAudio = (message.type === "ptt" || message.type === "audio") && message.hasMedia;

    if (isAudio) {
      const media = await message.downloadMedia();
      if (!media) {
        logDebug("Falha: downloadMedia retornou vazio.");
        await message.reply("Nao consegui baixar o audio.");
        return;
      }
      const payload = await sendAudioToAgent(media, phone);
      const text = payload?.transcription ? `Transcricao: ${payload.transcription}\n\n` : "";
      logDebug("Enviando resposta ao grupo", {
        hasTranscription: !!payload?.transcription,
        action: payload?.agent_result?.action,
        ok: payload?.agent_result?.ok,
      });
      await message.reply(text + formatAgentResponse(payload));
      return;
    }

    logDebug("Ignorada: mensagem no grupo permitido que nao eh audio.");
  } catch (err) {
    const detail = err?.response?.data?.detail;
    logDebug("Erro no processamento", {
      message: err?.message,
      detail,
      status: err?.response?.status,
    });
    await message.reply(`Erro ao processar: ${detail || err.message}`);
  }
}

client.on("message", async (message) => {
  await handleIncomingMessage(message, "message");
});

client.on("message_create", async (message) => {
  await handleIncomingMessage(message, "message_create");
});

client.initialize();
