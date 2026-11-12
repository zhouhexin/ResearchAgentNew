import {build} from 'esbuild'
import {parse,compileScript} from '@vue/compiler-sfc'
import {readFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
const cache=new Map()
globalThis.localStorage={getItem:k=>cache.get(k)||null,setItem:(k,v)=>cache.set(k,String(v)),removeItem:k=>cache.delete(k)}
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/render-entry.mjs'],outfile:'test-results/render-entry.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'vue-ssr',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{const source=await readFile(file,'utf8');const {descriptor}=parse(source.includes('<script')?source:'<script setup>const __renderSmoke=true</script>'+source,{filename:file});const code=compileScript(descriptor,{id:file,inlineTemplate:true,templateOptions:{ssr:true}});return{contents:code.content,loader:'ts',resolveDir:path.dirname(file)}})}}]})
await import(path.resolve('test-results/render-entry.mjs'))
