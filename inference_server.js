#!/usr/bin/env node
/**
 * GGUF 推理 HTTP 服务器
 * 基于正确的 llama-cpp 调用规范（user/assistant wrapper + 两轮收敛）
 * 
 * 关键：用户: 助手: wrapper，不是 system chat template
 */
import { createServer } from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODEL_PATH = process.argv[2] || path.join(__dirname, 'models/chinese-text-correction-1.5b.Q4_K_M.gguf');
const PORT = parseInt(process.argv[3] || '18789');

console.log(`[Inference] Model: ${MODEL_PATH}`);
console.log(`[Inference] Port: ${PORT}`);

// 正确的 prompt 格式：简单 instruction，不用 user/assistant wrapper
// node-llama-cpp 的 LlamaChatSession 内部有自己的 chat format，
// 加 user:/assistant: 前缀反而让模型以聊天模式输出，导致"真糟糕"而非"真不错"
function buildPrompt(text) {
    return `请纠正以下文本中的错别字和语法错误，只输出纠正后的文本：${text}`;
}

// 过滤模型输出中的残留 token
function filterResult(result) {
    result = result.trim().replace(/\r\n/g, '\n');
    // 去除模型可能返回的 <|im_end|> 残留
    if (result.includes('<|im_end|>')) {
        result = result.substring(0, result.indexOf('<|im_end|>')).trim();
    }
    if (result.includes('<|endoftext|>')) {
        result = result.substring(0, result.indexOf('<|endoftext|>')).trim();
    }
    return result || 'OK';
}

async function main() {
    const { getLlama, LlamaChatSession } = await import('/usr/local/install/global/node_modules/node-llama-cpp/dist/index.js');
    
    console.log('[Inference] Initializing llama...');
    const llama = await getLlama();
    
    console.log('[Inference] Loading model...');
    const model = await llama.loadModel({
        modelPath: MODEL_PATH,
        nCtx: 512,
        nThreads: 4,
        useMmap: true,
        gpu: 'auto',
    });
    console.log('[Inference] Model loaded!');
    
    const server = createServer(async (req, res) => {
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS, GET');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

        if (req.method === 'GET' && req.url === '/status') {
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({
                status: 'running',
                model: path.basename(MODEL_PATH),
                port: PORT,
                uptime: process.uptime(),
            }));
            return;
        }

        if (req.method === 'OPTIONS') {
            res.writeHead(204);
            res.end();
            return;
        }

        if (req.method !== 'POST') {
            res.writeHead(404, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Use POST /complete' }));
            return;
        }

        let body = '';
        let bodySize = 0;
        const MAX_BODY_SIZE = 1 * 1024 * 1024; // 1MB limit
        req.on('data', chunk => {
            bodySize += chunk.length;
            if (bodySize > MAX_BODY_SIZE) {
                res.writeHead(413, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ error: 'Request body too large' }));
                return;
            }
            body += chunk;
        });
        req.on('end', async () => {
            try {
                const { prompt, max_tokens = 2048, temperature = 0.3 } = JSON.parse(body);
                
                const context = await model.createContext();
                const session = new LlamaChatSession({
                    contextSequence: context.getSequence(),
                });
                
                // 第一轮纠错（instruction 格式，效果最佳）
                const response1 = await session.prompt(buildPrompt(prompt), {
                    maxTokens: max_tokens,
                    temperature: temperature,
                    stopStrings: ['<|im_end|>', '<|endoftext|>'],
                });
                const result1 = filterResult(response1);
                
                // 第二轮收敛（再纠错一次，效果更稳定）
                const response2 = await session.prompt(buildPrompt(result1), {
                    maxTokens: max_tokens,
                    temperature: temperature,
                    stopStrings: ['<|im_end|>', '<|endoftext|>'],
                });
                const finalResult = filterResult(response2);
                
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({
                    choices: [{ text: finalResult }]
                }));
            } catch (e) {
                console.error(`[Inference] Error: ${e.message}`);
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ error: e.message }));
            }
        });
    });

    server.listen(PORT, '127.0.0.1', () => {
        console.log(`[Inference] Server running on http://127.0.0.1:${PORT}`);
    });
}

main().catch(e => {
    console.error(`[Inference] Fatal: ${e.message}`);
    process.exit(1);
});