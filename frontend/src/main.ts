import { mount } from 'svelte';
import App from './App.svelte';
import { seedFirstVisit } from './lib/presets';
import './app.css';

// Before the app mounts, so a first visit's stores read the default preset.
seedFirstVisit();

export default mount(App, { target: document.getElementById('app')! });
