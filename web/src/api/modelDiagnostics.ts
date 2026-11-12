import type {ModelDiagnostic} from '../types/research'

export function modelStageName(diagnostic?:ModelDiagnostic):string {
 const stage=diagnostic?.network?.stage||''
 if(diagnostic?.network?.backend==='codex'){
  const names:Record<string,string>={connect:'建立 Codex 连接',tls:'Codex TLS 握手',websocket:'Codex WebSocket 连接',read:'读取 Codex 响应',response:'处理 Codex 响应'}
  if(diagnostic.phase&&names[diagnostic.phase])return names[diagnostic.phase]
 }
 if(stage==='codex.exec'||diagnostic?.phase==='codex')return 'Codex CLI 调用'
 if(stage.includes('connect_tcp'))return 'TCP 连接'
 if(stage.includes('start_tls'))return 'TLS 握手'
 if(stage==='requests.read')return '读取响应'
 if(stage==='requests.complete')return '响应已接收'
 const phases:Record<string,string>={connect:'建立连接',read:'读取响应',write:'发送请求',pool:'等待连接',response:'处理响应'}
 if(diagnostic?.phase&&phases[diagnostic.phase])return phases[diagnostic.phase]
 if(stage==='requests.send')return '发送请求或等待响应'
 return '服务未提供具体阶段'
}
