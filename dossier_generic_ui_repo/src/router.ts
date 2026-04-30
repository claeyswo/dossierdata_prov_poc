import { createRouter, createWebHistory } from 'vue-router'

import HomeView from './views/HomeView.vue'
import WorkflowHomeView from './views/WorkflowHomeView.vue'
import NewDossierView from './views/NewDossierView.vue'
import DossierView from './views/DossierView.vue'

// Route hierarchy is:
//
//   /                           workflow picker (lists all workflows)
//   /workflows/:wf              workflow home — search results + new btn
//   /workflows/:wf/dossiers/new pick a can_create_dossier activity
//   /workflows/:wf/dossiers/:id existing-dossier OR pre-creation orchestrator
//
// The :wf param is the workflow's name (e.g. "toelatingen"), matching
// the engine's workflow plugin registry. Components read it via
// route.params.wf.
//
// :id at the dossier level is the dossier's UUID. The DossierView
// also reads ?init=workflow:activity for pre-creation mode (a creation
// activity was selected and we're rendering its form against a
// dossier that doesn't yet exist).

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/',
      name: 'home',
      component: HomeView },

    { path: '/workflows/:wf',
      name: 'workflow-home',
      component: WorkflowHomeView,
      props: true },

    { path: '/workflows/:wf/dossiers/new',
      name: 'new-dossier',
      component: NewDossierView,
      props: true },

    { path: '/workflows/:wf/dossiers/:id',
      name: 'dossier',
      component: DossierView,
      props: true },

    // Default redirect: anything else lands on the home picker.
    { path: '/:catchAll(.*)', redirect: '/' },
  ],
})
