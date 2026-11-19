import {build} from 'esbuild'
import {parse,compileScript} from '@vue/compiler-sfc'
import {readFile,mkdir} from 'node:fs/promises'
import path from 'node:path'
await mkdir('test-results',{recursive:true})
await build({entryPoints:['scripts/planning-entry.mjs'],outfile:'test-results/planning.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'planning-vue-interaction',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{
 if(file.endsWith('/ExecutionReviewLinks.vue')||file.endsWith('/QuestionCoverageWorkspace.vue'))return{contents:"import {h} from 'vue';export default {setup:()=>()=>h('section','覆盖检查入口')}",loader:'js'}
 if(file.endsWith('/TaskProgress.vue'))return{contents:"import {h} from 'vue';export default {props:['task'],setup:p=>()=>h('p',p.task.stage+' '+(p.task.error?.message||''))}",loader:'js'}
 const {descriptor}=parse(await readFile(file,'utf8'),{filename:file})
 return{contents:compileScript(descriptor,{id:file,inlineTemplate:true}).content,loader:'ts',resolveDir:path.dirname(file)}
})}}]})
await import(path.resolve('test-results/planning.mjs'))
await build({entryPoints:['scripts/research-search-entry.mjs'],outfile:'test-results/research-search.mjs',bundle:true,platform:'node',format:'esm',packages:'external',define:{'import.meta.env':'{}'},plugins:[{name:'search-vue-interaction',setup(b){b.onLoad({filter:/\.vue$/},async({path:file})=>{
 if(file.endsWith('/ExecutionReviewLinks.vue')||file.endsWith('/QuestionCoverageWorkspace.vue'))return{contents:"import {h} from 'vue';export default {setup:()=>()=>h('section','覆盖检查入口')}",loader:'js'}
 if(file.endsWith('/TaskProgress.vue'))return{contents:"import {h} from 'vue';export default {props:['task'],setup:p=>()=>h('p',p.task.stage+' '+(p.task.error?.message||''))}",loader:'js'}
 const {descriptor}=parse(await readFile(file,'utf8'),{filename:file})
 return{contents:compileScript(descriptor,{id:file,inlineTemplate:true}).content,loader:'ts',resolveDir:path.dirname(file)}
})}}]})
await import(path.resolve('test-results/research-search.mjs'))
