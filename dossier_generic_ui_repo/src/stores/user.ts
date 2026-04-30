// User identity store. The platform's POC auth is "send X-POC-User
// with the username and the engine looks up roles from the YAML's
// poc_users block". This store holds the active username; switching
// users is a single dropdown click.
//
// The choice of which usernames to offer is hard-coded against
// toelatingen for the demo. A real frontend would hit an
// authentication endpoint instead of letting the user pick a role.
//
// Persisted to localStorage so the picked user survives reloads —
// without that, every refresh resets to the default and the demo
// gets confusing fast.

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

const STORAGE_KEY = 'dossier-ui:username'

// Hard-coded for the demo. Each entry is a username that exists in
// toelatingen's poc_users list; the engine looks up roles from there.
// Picking different users exercises different authorization branches.
//
// In the workflow these users have these roles:
//   - jan.aanvrager  → oe:aanvrager (kan aanvragen indienen)
//   - claeyswo       → beheerder (kan exceptions verlenen, tombstones,
//                                  verantwoordelijke organisatie aanduiden)
//   - marie.brugge   → behandelaar + beslisser (gemeente Brugge — kan
//                                  beslissingen tekenen voor Brugse aanvragen)
//   - sophie.tekent  → behandelaar (kan dossiers behandelen)
//
// The labels tell users in plain Dutch what each user can do, so a
// demo-er knows which to pick to exercise a given step in the flow.
export const KNOWN_USERS = [
  { username: 'jan.aanvrager', label: 'Jan (aanvrager)' },
  { username: 'firma.acme',    label: 'ACME (aanvrager)' },
  { username: 'claeyswo',      label: 'Wouter (beheerder)' },
  { username: 'marie.brugge',  label: 'Marie (beslisser, gemeente Brugge)' },
  { username: 'benjamma',      label: 'Matthias (behandelaar)' },
  { username: 'sophie.tekent', label: 'Sophie (behandelaar)' },
] as const

export const useUserStore = defineStore('user', () => {
  const stored = (typeof localStorage !== 'undefined')
    ? localStorage.getItem(STORAGE_KEY)
    : null

  const username = ref<string>(stored ?? KNOWN_USERS[0].username)

  watch(username, (v) => {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(STORAGE_KEY, v)
    }
  })

  function set(name: string) {
    username.value = name
  }

  return { username, set }
})
