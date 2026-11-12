export function parseCitation(value:string):{evidenceId:string;paperId?:number}|null {const m=/^(?:P([1-9]\d*):)?(E[1-9]\d*)$/.exec(value);return m?{evidenceId:m[2]!,paperId:m[1]?Number(m[1]):undefined}:null}
export function safeUrl(value:string|null|undefined):string|null {if(!value||!(/^(https?:\/\/|\/)/i.test(value)))return null;try{const u=new URL(value,globalThis.location?.origin||'http://localhost');return ['http:','https:'].includes(u.protocol)?u.href:null}catch{return null}}
export function validYears(start?:number|null,end?:number|null){return !(start&&end&&start>end)}
export function csvCell(value:unknown){let text=String(value??'');if(/^[=+@\-\t\r]/.test(text))text="'"+text;return '"'+text.replaceAll('"','""')+'"'}
export function download(content:string,name:string,type='application/json'){const url=URL.createObjectURL(new Blob([content],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
export function readStorage<T>(key:string,fallback:T):T{try{const s=localStorage.getItem(key);return s?JSON.parse(s):fallback}catch{return fallback}}
export function cleanParams(params?:Record<string,unknown>):Record<string,unknown>|undefined {
  if (!params) return undefined
  return Object.fromEntries(Object.entries(params).filter(([,value]) =>
    value !== '' && value !== null && value !== undefined,
  ))
}
