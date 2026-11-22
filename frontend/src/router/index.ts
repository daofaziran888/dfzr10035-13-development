import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import CollectionView from '@/views/CollectionView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/collection', name: 'collection', component: CollectionView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
