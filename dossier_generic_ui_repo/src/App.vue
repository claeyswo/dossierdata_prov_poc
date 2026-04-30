<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore, KNOWN_USERS } from './stores/user'
import { useRoute, useRouter } from 'vue-router'

// Workflow context for the navigation bar.
//
// The route's :wf param drives nav state. When we're inside a
// workflow (any /workflows/:wf/* route), we surface that workflow's
// "Mijn dossiers" + "Nieuw dossier" links in the strip. Outside any
// workflow (the home picker), we show only "Home" — there's nothing
// dossier-related to navigate to until the user picks a workflow.

const userStore = useUserStore()
const route = useRoute()
const router = useRouter()

const currentWorkflow = computed<string | null>(() => {
  const w = route.params.wf
  return typeof w === 'string' && w.length > 0 ? w : null
})

function navHome() { router.push({ name: 'home' }) }
</script>

<template>
  <div class="app-shell">
    <!-- White masthead with brand wordmark. Mirrors the screenshot:
         large purple title on the left, tagline-style mark on the right. -->
    <header class="masthead">
      <h1 class="brand-title" @click="navHome" style="cursor: pointer;">
        Dossier register
        <small>Onroerend Erfgoed · Generieke werkruimte</small>
      </h1>
      <div class="vlaanderen-mark">
        <strong>Vlaanderen</strong>
        is erfgoed
      </div>
    </header>

    <!-- Saturated purple nav strip. Items shift based on whether
         we're inside a workflow context. -->
    <nav class="nav-strip">
      <RouterLink :to="{ name: 'home' }" class="home" exact-active-class="active">
        ⌂&nbsp; Home
      </RouterLink>
      <template v-if="currentWorkflow">
        <RouterLink
          :to="{ name: 'workflow-home', params: { wf: currentWorkflow } }"
          active-class="active"
        >
          Mijn dossiers
        </RouterLink>
        <RouterLink
          :to="{ name: 'new-dossier', params: { wf: currentWorkflow } }"
          active-class="active"
        >
          Nieuw dossier
        </RouterLink>
      </template>

      <div class="right">
        <span class="user-pill">Aangemeld als</span>
        <select
          :value="userStore.username"
          @change="userStore.set(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="u in KNOWN_USERS" :key="u.username" :value="u.username">
            {{ u.label }}
          </option>
        </select>
      </div>
    </nav>

    <main class="app-main">
      <!--
        :key forces a remount when the user identity changes.

        Why this works: the engine's responses depend on which user
        is making the request — allowedActivities, search ACL,
        entity visibility — so flipping the user dropdown silently
        leaves stale data on screen until the user navigates. Keying
        the RouterView on ``username`` makes Vue treat the user-switch
        as a "different component" event and remount, which re-runs
        each view's onMounted load.

        Including ``route.fullPath`` is belt-and-braces: route changes
        already remount via Vue Router's normal dispatch, but a
        hand-rolled :key is now required for that case too because
        we've taken over key control. Concatenating both with a
        delimiter is enough — no need for query-param awareness on
        top, since fullPath already includes the query string.

        Trade-off: form state and scroll position reset on user
        switch. Acceptable here since switching users mid-form is
        almost certainly a sign that the in-progress submission
        belongs to a different user anyway, and we'd rather not
        accidentally submit one user's draft as another user.
      -->
      <RouterView :key="userStore.username + '|' + route.fullPath" />
    </main>
  </div>
</template>
