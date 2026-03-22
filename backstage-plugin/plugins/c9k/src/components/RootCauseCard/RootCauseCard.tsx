import React from 'react';
import {
  Card,
  CardContent,
  CardHeader,
  Chip,
  LinearProgress,
  Typography,
} from '@material-ui/core';
import { makeStyles } from '@material-ui/core/styles';
import ErrorOutlineIcon from '@material-ui/icons/ErrorOutline';
import CheckCircleOutlineIcon from '@material-ui/icons/CheckCircleOutline';
import { Diagnosis } from '../../api/types';

const useStyles = makeStyles(theme => ({
  card: {
    marginBottom: theme.spacing(2),
  },
  confidence: {
    marginBottom: theme.spacing(1),
  },
  confidenceBar: {
    height: 8,
    borderRadius: 4,
  },
  causalPath: {
    fontFamily: 'monospace',
    fontSize: '0.85rem',
    padding: theme.spacing(1),
    backgroundColor: theme.palette.background.default,
    borderRadius: 4,
    marginTop: theme.spacing(1),
    overflowX: 'auto',
  },
  competingList: {
    marginTop: theme.spacing(1),
  },
  chip: {
    margin: theme.spacing(0.5),
  },
}));

function confidenceColor(c: number): 'primary' | 'secondary' {
  return c >= 0.8 ? 'secondary' : 'primary';
}

function rootCauseLabel(rootCause: string): string {
  // commit://org/repo/abc123 (CodeChange) → "abc123 (CodeChange)"
  const match = rootCause.match(/\/([a-f0-9]{7,8})\s*(\(.+\))?$/);
  if (match) return `${match[1]}${match[2] ? ` ${match[2]}` : ''}`;
  // latent://flaky-tests (FlakyTestRun) → "Flaky Tests"
  if (rootCause.includes('flaky-tests')) return 'Flaky Tests';
  if (rootCause.includes('azure-oidc')) return 'Azure OIDC';
  if (rootCause.includes('ghcr')) return 'GHCR';
  if (rootCause.includes('runner-env')) return 'Runner Environment';
  return rootCause;
}

interface RootCauseCardProps {
  diagnosis: Diagnosis;
}

export function RootCauseCard({ diagnosis }: RootCauseCardProps) {
  const classes = useStyles();
  const pct = Math.round(diagnosis.confidence * 100);
  const isHealthy = !diagnosis.root_cause;

  const icon = isHealthy ? (
    <CheckCircleOutlineIcon style={{ color: '#4caf50' }} />
  ) : (
    <ErrorOutlineIcon color="error" />
  );

  const title = isHealthy
    ? 'No issues detected'
    : rootCauseLabel(diagnosis.root_cause!);

  const subheader = isHealthy
    ? diagnosis.target_node
    : `${pct}% confidence — ${diagnosis.target_node}`;

  return (
    <Card className={classes.card} variant="outlined">
      <CardHeader avatar={icon} title={title} subheader={subheader} />
      {!isHealthy && (
        <CardContent>
          <div className={classes.confidence}>
            <Typography variant="body2" color="textSecondary" gutterBottom>
              Confidence
            </Typography>
            <LinearProgress
              variant="determinate"
              value={pct}
              color={confidenceColor(diagnosis.confidence)}
              className={classes.confidenceBar}
            />
          </div>

          {diagnosis.causal_path.length > 0 && (
            <>
              <Typography variant="body2" color="textSecondary">
                Causal Path
              </Typography>
              <div className={classes.causalPath}>
                {diagnosis.causal_path.join(' → ')}
              </div>
            </>
          )}

          {diagnosis.competing_causes.length > 0 && (
            <div className={classes.competingList}>
              <Typography variant="body2" color="textSecondary" gutterBottom>
                Competing Causes
              </Typography>
              {diagnosis.competing_causes.map(([cause, conf]) => (
                <Chip
                  key={cause}
                  label={`${rootCauseLabel(cause)} (${Math.round(conf * 100)}%)`}
                  size="small"
                  variant="outlined"
                  className={classes.chip}
                />
              ))}
            </div>
          )}
        </CardContent>
      )}
    </Card>
  );
}
