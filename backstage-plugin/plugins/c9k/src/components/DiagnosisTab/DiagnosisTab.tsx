import React from 'react';
import { Grid, Typography } from '@material-ui/core';
import { makeStyles } from '@material-ui/core/styles';
import Alert from '@material-ui/lab/Alert';
import {
  InfoCard,
  Progress,
  ResponseErrorPanel,
} from '@backstage/core-components';
import { useApi } from '@backstage/core-plugin-api';
import { useEntity } from '@backstage/plugin-catalog-react';
import { c9kApiRef } from '../../api';
import { C9K_REPO_ANNOTATION } from '../../api/types';
import { RootCauseCard } from '../RootCauseCard';
import { AlertGroupCard } from '../AlertGroupCard';
import { useAsync } from 'react-use';

const useStyles = makeStyles(theme => ({
  section: {
    marginBottom: theme.spacing(3),
  },
}));

export function DiagnosisTab() {
  const classes = useStyles();
  const { entity } = useEntity();
  const c9k = useApi(c9kApiRef);

  const repo =
    entity.metadata.annotations?.[C9K_REPO_ANNOTATION] ??
    entity.metadata.annotations?.['github.com/project-slug'];

  const diagnoses = useAsync(() => c9k.getAllDiagnoses(), [c9k]);
  const alertGroups = useAsync(() => c9k.getAlertGroups(), [c9k]);

  if (diagnoses.loading || alertGroups.loading) {
    return <Progress />;
  }

  if (diagnoses.error) {
    return <ResponseErrorPanel error={diagnoses.error} />;
  }

  // Filter diagnoses to this repo if we have an annotation
  const allDiagnoses = diagnoses.value ?? [];
  const filtered = repo
    ? allDiagnoses.filter(d => d.target_node.includes(repo))
    : allDiagnoses;

  const groups = alertGroups.value ?? [];
  const filteredGroups = repo
    ? groups.filter(g =>
        g.root_cause.includes(repo) ||
        g.members.some(m => m.includes(repo)),
      )
    : groups;

  const hasIssues = filtered.length > 0 || filteredGroups.length > 0;

  return (
    <Grid container spacing={3}>
      {!hasIssues && (
        <Grid item xs={12}>
          <Alert severity="success">
            No active CI failures or incidents detected
            {repo ? ` for ${repo}` : ''}.
          </Alert>
        </Grid>
      )}

      {filteredGroups.length > 0 && (
        <Grid item xs={12}>
          <div className={classes.section}>
            <InfoCard title="Correlated Failure Groups">
              <Typography variant="body2" color="textSecondary" paragraph>
                Failures grouped by shared root cause. Each group represents
                a single upstream issue affecting multiple jobs.
              </Typography>
              {filteredGroups.map(group => (
                <AlertGroupCard key={group.root_cause} group={group} />
              ))}
            </InfoCard>
          </div>
        </Grid>
      )}

      {filtered.length > 0 && (
        <Grid item xs={12}>
          <div className={classes.section}>
            <InfoCard title="Root Cause Analysis">
              <Typography variant="body2" color="textSecondary" paragraph>
                Individual diagnoses ranked by confidence. Higher confidence
                means stronger evidence linking the root cause to the failure.
              </Typography>
              {filtered.map(d => (
                <RootCauseCard key={d.target_node} diagnosis={d} />
              ))}
            </InfoCard>
          </div>
        </Grid>
      )}
    </Grid>
  );
}
