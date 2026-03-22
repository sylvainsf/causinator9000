import React from 'react';
import {
  Card,
  CardContent,
  CardHeader,
  Chip,
  List,
  ListItem,
  ListItemText,
  Typography,
} from '@material-ui/core';
import { makeStyles } from '@material-ui/core/styles';
import GroupWorkIcon from '@material-ui/icons/GroupWork';
import { AlertGroup } from '../../api/types';

const useStyles = makeStyles(theme => ({
  card: {
    marginBottom: theme.spacing(2),
  },
  memberList: {
    maxHeight: 200,
    overflow: 'auto',
  },
  chip: {
    marginLeft: theme.spacing(1),
  },
}));

function formatNodeId(id: string): string {
  // job://org/repo/12345/job-name → "job-name (run 12345)"
  const jobMatch = id.match(/job:\/\/[^/]+\/[^/]+\/(\d+)\/(.+)/);
  if (jobMatch) return `${jobMatch[2]} (run ${jobMatch[1]})`;
  return id;
}

interface AlertGroupCardProps {
  group: AlertGroup;
}

export function AlertGroupCard({ group }: AlertGroupCardProps) {
  const classes = useStyles();
  const pct = Math.round(group.confidence * 100);

  return (
    <Card className={classes.card} variant="outlined">
      <CardHeader
        avatar={<GroupWorkIcon color="error" />}
        title={group.root_cause}
        subheader={
          <>
            {pct}% confidence
            {group.mutation_type && (
              <Chip
                label={group.mutation_type}
                size="small"
                variant="outlined"
                className={classes.chip}
              />
            )}
          </>
        }
        action={
          <Chip
            label={`${group.members.length} affected`}
            color="secondary"
            size="small"
          />
        }
      />
      <CardContent>
        <Typography variant="body2" color="textSecondary" gutterBottom>
          Affected Jobs
        </Typography>
        <List dense className={classes.memberList}>
          {group.members.map(member => (
            <ListItem key={member}>
              <ListItemText primary={formatNodeId(member)} />
            </ListItem>
          ))}
        </List>
      </CardContent>
    </Card>
  );
}
