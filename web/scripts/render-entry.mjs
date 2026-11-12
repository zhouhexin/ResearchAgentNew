import {createSSRApp} from 'vue'
import {createPinia} from 'pinia'
import {createRouter,createMemoryHistory} from 'vue-router'
import {renderToString} from '@vue/server-renderer'
import ElementPlus,{ID_INJECTION_KEY,ZINDEX_INJECTION_KEY} from 'element-plus'
import Dashboard from '../src/views/DashboardView.vue'
import Search from '../src/views/SearchView.vue'
import Library from '../src/views/LibraryView.vue'
import Paper from '../src/views/PaperDetailView.vue'
import Agent from '../src/views/AgentView.vue'
import Analysis from '../src/views/AnalysisView.vue'
import Review from '../src/views/ReviewView.vue'
import Tasks from '../src/views/TaskCenterView.vue'
import Settings from '../src/views/SettingsView.vue'
import NotFound from '../src/views/NotFoundView.vue'
import Citation from '../src/components/EvidenceCitation.vue'
import seed from '../src/data/seed.json'
import AnalysisCard from '../src/components/AnalysisCard.vue'
import TaskProgress from '../src/components/TaskProgress.vue'
import ReviewReportBody from '../src/components/ReviewReportBody.vue'
const failedModelCheck={id:'diagnostic-test',type:'model_diagnostic',status:'failed',progress:10,stage:'模型检测失败',model_name:'test-model',http_backend:'sdk',result_id:null,created_at:new Date().toISOString(),updated_at:new Date().toISOString(),error:{code:'MODEL_CONNECTION_ERROR',message:'连接被重置',diagnostic:{phase:'connect',host:'model.example.org',network:{backend:'sdk',stage:'connection.start_tls.failed'},causes:['ConnectError','ConnectionResetError']}}}
const failedPdfDownload={...failedModelCheck,id:'pdf-diagnostic-test',type:'document_parse',model_name:undefined,http_backend:undefined,download_progress:{candidate_found:true,response_received:true,pdf_header_verified:true,http_status:200,host:'files.example.org',provider:'arXiv',received_bytes:1024,total_bytes:4096,elapsed_seconds:150,average_bytes_per_second:6.8},error:{code:'PDF_DOWNLOAD_TIMEOUT',message:'总时限到期',diagnostic:{host:'files.example.org',phase:'download',received_bytes:1024,total_bytes:4096,elapsed_seconds:150,total_timeout:120,read_timeout:60}}}
const detailedDraft={title:'详细综述',background:[],common_methods:[],comparisons:[{heading:'结合实验条件理解结果',claim:'主题句说明比较方向。',evidence_refs:['P2:E21','P3:E46'],paragraphs:[{text:'这里解释输入、方法步骤以及结果的适用条件，帮助读者理解指标背后的含义。',evidence_refs:['P2:E21']}]}],limitations:[],synthesis:[{claim:'围绕研究问题给出有条件的综合判断。',evidence_refs:['P2:E21','P3:E46']}],tentative_directions:[]}
let failures=0
for(const [name,component,path,props] of [['Dashboard',Dashboard,'/'],['Search',Search,'/search'],['Library',Library,'/library'],['Paper',Paper,'/papers/2'],['Agent',Agent,'/agent'],['Analysis',Analysis,'/analysis'],['Review',Review,'/reviews'],['Tasks',Tasks,'/tasks'],['Settings',Settings,'/settings'],['NotFound',NotFound,'/missing'],['Citation',Citation,'/',{refId:'P2:E21'}],['AnalysisCard',AnalysisCard,'/',{title:'研究方法',findings:seed.analyses[0].analysis.methodologies,paperId:2}],['ModelDiagnostic',TaskProgress,'/settings',{task:failedModelCheck}],['PdfDownloadDiagnostic',TaskProgress,'/tasks',{task:failedPdfDownload}],['DetailedReview',ReviewReportBody,'/reviews',{draft:detailedDraft}],['LegacyReview',ReviewReportBody,'/reviews',{draft:seed.review}]]){
 try{const router=createRouter({history:createMemoryHistory(),routes:[{path:'/papers/:paperId',component:Paper},{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push(path);await router.isReady();const app=createSSRApp(component,props).use(createPinia()).use(router).use(ElementPlus).provide(ID_INJECTION_KEY,{prefix:100,current:0}).provide(ZINDEX_INJECTION_KEY,{current:0});const html=await renderToString(app);if(html.length<30)throw Error('Empty render');if(name==='Citation'&&!html.includes('P2:E21'))throw Error('Missing citation');if(name==='Settings'&&(!html.includes('模型连接检测')||!html.includes('演示模式不发送模型请求')))throw Error('Missing model check panel or demo restriction');if(name==='ModelDiagnostic'&&(!html.includes('TLS 握手')||!html.includes('model.example.org')||!html.includes('查看连接诊断')))throw Error('Missing network diagnostics');if(name==='PdfDownloadDiagnostic'&&(!html.includes('已确认 PDF 文件头')||!html.includes('查看 PDF 下载诊断')||!html.includes('files.example.org')||!html.includes('接收 PDF')||html.includes('模型服务')))throw Error('PDF diagnostics missing or mislabeled as model');if(name==='DetailedReview'&&(!html.includes('结合实验条件理解结果')||!html.includes('这里解释输入、方法步骤')||!html.includes('综合判断')||!html.includes('P2:E21')))throw Error('Missing detailed explanations or citations');if(name==='LegacyReview'&&html.includes('围绕研究问题的综合回答'))throw Error('Legacy report unexpectedly changed');console.log('PASS',name)}catch(e){failures++;console.error('FAIL',name,e.message)}}
if(failures)process.exitCode=1
