import {test} from 'node:test'
import assert from 'node:assert/strict'
import {modelStageName} from '../src/api/modelDiagnostics.ts'

test('优先显示实际 TCP/TLS 追踪阶段',()=>{
 assert.equal(modelStageName({phase:'connect',network:{stage:'connection.start_tls.failed'}}),'TLS 握手')
 assert.equal(modelStageName({network:{stage:'connection.connect_tcp.failed'}}),'TCP 连接')
})
test('requests 未收到响应头前保留连接或响应阶段的不确定性',()=>{
 assert.equal(modelStageName({network:{stage:'requests.send'}}),'发送请求或等待响应')
 assert.equal(modelStageName({phase:'connect',network:{stage:'requests.send'}}),'建立连接')
 assert.equal(modelStageName({network:{stage:'requests.read'}}),'读取响应')
})
test('缺少诊断时不猜测失败阶段',()=>{
 assert.equal(modelStageName(), '服务未提供具体阶段')
 assert.equal(modelStageName({phase:'unknown'}), '服务未提供具体阶段')
})
test('Codex 显示已确认的连接阶段',()=>{
 assert.equal(modelStageName({phase:'tls',network:{backend:'codex',stage:'codex.tls'}}),'Codex TLS 握手')
 assert.equal(modelStageName({phase:'websocket',network:{backend:'codex'}}),'Codex WebSocket 连接')
})
