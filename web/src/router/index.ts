import {createRouter,createWebHistory} from 'vue-router'
export const router=createRouter({history:createWebHistory(),scrollBehavior:()=>({top:0}),routes:[
{path:'/',component:()=>import('../views/DashboardView.vue'),meta:{title:'工作台',eyebrow:'WORKSPACE'}},
{path:'/search',component:()=>import('../views/SearchView.vue'),meta:{title:'文献检索',eyebrow:'DISCOVER'}},
{path:'/library',component:()=>import('../views/LibraryView.vue'),meta:{title:'我的文献库',eyebrow:'LIBRARY'}},
{path:'/papers/:paperId',component:()=>import('../views/PaperDetailView.vue'),meta:{title:'论文详情',eyebrow:'PAPER'}},
{path:'/agent',component:()=>import('../views/AgentView.vue'),meta:{title:'科研 Agent',eyebrow:'RESEARCH ASSISTANT'}},
{path:'/analysis',component:()=>import('../views/AnalysisView.vue'),meta:{title:'论文分析',eyebrow:'PAPER ANALYSIS'}},
{path:'/reviews',component:()=>import('../views/ReviewView.vue'),meta:{title:'文献综述',eyebrow:'LITERATURE REVIEW'}},
{path:'/evaluation',component:()=>import('../views/EvaluationView.vue'),meta:{title:'研究评测',eyebrow:'RESEARCH EVALUATION'}},
{path:'/tasks',component:()=>import('../views/TaskCenterView.vue'),meta:{title:'任务中心',eyebrow:'ACTIVITY'}},
{path:'/settings',component:()=>import('../views/SettingsView.vue'),meta:{title:'设置',eyebrow:'PREFERENCES'}},
{path:'/:pathMatch(.*)*',component:()=>import('../views/NotFoundView.vue'),meta:{title:'页面未找到'}}]})
