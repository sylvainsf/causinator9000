import React from 'react';
import { createDevApp } from '@backstage/dev-utils';
import { c9kPlugin, EntityC9kDiagnosisTab } from '../src/plugin';

createDevApp()
  .registerPlugin(c9kPlugin)
  .addPage({
    element: <EntityC9kDiagnosisTab />,
    title: 'C9K Diagnosis',
    path: '/c9k',
  })
  .render();
