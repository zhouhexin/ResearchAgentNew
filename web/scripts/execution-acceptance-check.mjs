import {build} from 'esbuild'
import {parse,compileScript} from '@vue/compiler-sfc'
import {readFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
import {pathToFileURL} from 'node:url'
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/execution-acceptance-entry.mjs'],outfile:'test-results/execution-acceptance.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'vue-interaction',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{
 if(file.endsWith('/EvidenceCitation.vue')||file.endsWith('/ExecutionResults.vue')||file.endsWith('/PlanReportWorkspace.vue')||file.endsWith('/ReadingCoverage.vue'))return{contents:"import {h} from 'vue';export default {props:['refId'],setup:p=>()=>h('span',p.refId)}",loader:'js'}
 if(file.endsWith('/TaskProgress.vue'))return{contents:"import {h} from 'vue';export default {props:['task'],setup:p=>()=>h('p',p.task.stage+' '+(p.task.error?.message||''))}",loader:'js'}
 const source=await readFile(file,'utf8'),{descriptor}=parse(source,{filename:file})
 const code=compileScript(descriptor,{id:file,inlineTemplate:true})
 return{contents:code.content,loader:'ts',resolveDir:path.dirname(file)}
})}}]})
await import(pathToFileURL(path.resolve('test-results/execution-acceptance.mjs')).href)
