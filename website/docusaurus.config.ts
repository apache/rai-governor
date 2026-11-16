import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const isGitHubPages = process.env.FOR_GH_PAGES === 'true' || process.env.FOR_GH_PAGES === '1';

const config: Config = {
  title: 'Governor',
  tagline: 'A web app for rapidly prototyping AI agents and the lightweight web UIs that wrap them—build flows, preview interactions, and share agent-driven experiences without lock-in.',
  favicon: 'img/favicon.ico',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Set the production url of your site here
  url: isGitHubPages ? 'https://apache.github.io' : 'https://your-docusaurus-site.example.com',
  // Set the /<baseUrl>/ pathname under which your site is served
  // For GitHub pages deployment, it is often '/<projectName>/'
  baseUrl: isGitHubPages ? '/rai-governor/' : '/',

  // GitHub pages deployment config.
  // If you aren't using GitHub pages, you don't need these.
  organizationName: 'apache', // Usually your GitHub org/user name.
  projectName: 'rai-governor', // Usually your repo name.

  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. For example, if your site is Chinese, you
  // may want to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        gtag: {
          trackingID: 'G-S4YL0CG31S',
          anonymizeIP: true,
        },
        docs: {
          sidebarPath: './sidebars.ts',
          // Please change this to your repo.
          // Remove this to remove the "edit this page" links.
          // editUrl:
          //   'https://github.com/facebook/docusaurus/tree/main/packages/create-docusaurus/templates/shared/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // Replace with your project's social card
    image: 'img/governor-icon-512.png',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Governor',
      logo: {
        alt: 'Governor',
        src: 'img/governor-symbol.svg',
        srcDark: 'img/governor-symbol-dark.svg',
      },
      items: [
        {
          to: '/',
          label: 'Home',
          position: 'left',
        },
        {
          type: 'doc',
          docId: 'quickstart/index',
          label: 'Quickstart',
          position: 'left',
        },
        {
          label: 'Developers',
          position: 'left',
          items: [
            {
              type: 'doc',
              docId: 'developers-quickstart',
              label: 'Developer Quickstart',
            },
            {
              type: 'doc',
              docId: 'api',
              label: 'API Reference',
            },
            {
              type: 'doc',
              docId: 'architecture',
              label: 'Architecture / Services',
            },
            {
              type: 'doc',
              docId: 'llm-provider-configuration',
              label: 'LLM Provider Configuration',
            },
            {
              type: 'doc',
              docId: 'observability',
              label: 'Observability',
            },
            {
              type: 'doc',
              docId: 'testing/index',
              label: 'Testing',
            },
            {
              type: 'doc',
              docId: 'testing/contributing',
              label: 'Contributing',
            },
          ],
        },
        {
          label: 'Project',
          position: 'left',
          items: [
            {
              type: 'doc',
              docId: 'about-name-origin',
              label: 'About / Name Origin',
            },
            {
              type: 'doc',
              docId: 'roadmap',
              label: 'Roadmap',
            },
            {
              type: 'doc',
              docId: 'contact-community',
              label: 'Contact / Community',
            },
            {
              type: 'doc',
              docId: 'license',
              label: 'License',
            },
          ],
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Quickstart',
              to: '/docs/quickstart',
            },
          ],
        },
        {
          title: 'Community',
          items: [
            // {
            //   label: 'Stack Overflow',
            //   href: 'https://stackoverflow.com/questions/tagged/docusaurus',
            // },
            // {
            //   label: 'Discord',
            //   href: 'https://discordapp.com/invite/docusaurus',
            // },
            // {
            //   label: 'X',
            //   href: 'https://x.com/docusaurus',
            // },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/apache/rai-governor',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} The Apache Software Foundation. Licensed under the Apache License, Version 2.0.<br/>Apache and the Apache logo are trademarks of The Apache Software Foundation.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.nightOwl,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
