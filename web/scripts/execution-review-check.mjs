import {build} from 'esbuild'
import {parse,compileScript} from '@vue/compiler-sfc'
import {readFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/execution-review-entry.mjs'],outfile:'test-results/execution-review.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'vue-interaction',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{
 if(file.endsWith('/PlanReportWorkspace.vue')||file.endsWith('/ReadingCoverage.vue'))return{contents:"import {h} from 'vue';export default {props:['refId'],setup:p=>()=>h('span',p.refId)}",loader:'js'}
 if(file.endsWith('/ExecutionAcceptance.vue')||file.endsWith('/ExecutionReviewLinks.vue')||file.endsWith('/QuestionCoverageWorkspace.vue'))return{contents:"import {h} from 'vue';export default {setup:()=>()=>h('section','覆盖检查入口')}",loader:'js'}
 if(file.endsWith('/TaskProgress.vue'))return{contents:"import {h} from 'vue';export default {props:['task'],setup:p=>()=>h('p',p.task.stage+' '+(p.task.error?.message||''))}",loader:'js'}
 const source=await readFile(file,'utf8'),{descriptor}=parse(source,{filename:file})
 const code=compileScript(descriptor,{id:file,inlineTemplate:true})
 return{contents:code.content,loader:'ts',resolveDir:path.dirname(file)}
})}}]})
await import(path.resolve('test-results/execution-review.mjs'))
