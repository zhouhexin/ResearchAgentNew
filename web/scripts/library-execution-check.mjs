import {build} from 'esbuild'
import {parse,compileScript} from '@vue/compiler-sfc'
import {readFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/library-execution-entry.mjs'],outfile:'test-results/library-execution.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'vue-interaction',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{
 if(file.endsWith('/ExecutionResults.vue')||file.endsWith('/PlanReportWorkspace.vue')||file.endsWith('/ReadingCoverage.vue'))return{contents:"import {h} from 'vue';export default {props:['refId'],setup:p=>()=>h('span',p.refId)}",loader:'js'}
 if(file.endsWith('/TaskProgress.vue'))return{contents:"import {h} from 'vue';export default {props:['task'],setup:p=>()=>h('p',p.task.stage+' '+(p.task.error?.message||''))}",loader:'js'}
 const source=await readFile(file,'utf8'),{descriptor}=parse(source,{filename:file})
 const code=compileScript(descriptor,{id:file,inlineTemplate:true})
 return{contents:code.content,loader:'ts',resolveDir:path.dirname(file)}
})}}]})
await import(path.resolve('test-results/library-execution.mjs'))
