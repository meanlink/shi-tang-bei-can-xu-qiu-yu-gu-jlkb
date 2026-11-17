import {api} from './client.js';

const base = '/blind';

export const blind = {
  listCampaigns: () => api(base + '/campaigns'),
  getCampaign: id => api(base + '/campaigns/' + id),
  createCampaign: data => api(base + '/campaigns', {method: 'POST', body: JSON.stringify(data)}),
  closeCampaign: id => api(base + '/campaigns/' + id + '/close', {method: 'POST'}),
  prelockWindow: id => api(base + '/campaigns/' + id + '/prelock-window', {method: 'POST'}),
  lockForecast: (id, payload) => api(base + '/campaigns/' + id + '/lock-forecast',
    {method: 'POST', body: JSON.stringify(payload)}),
  reportActual: (id, payload) => api(base + '/campaigns/' + id + '/actual',
    {method: 'POST', body: JSON.stringify(payload)}),
  rescore: id => api(base + '/campaigns/' + id + '/rescore', {method: 'POST'}),
  promote: id => api(base + '/campaigns/' + id + '/promote', {method: 'POST'}),
  promotions: id => api(base + '/campaigns/' + id + '/promotions'),
  listTrials: () => api(base + '/trials'),
  createTrial: data => api(base + '/trials', {method: 'POST', body: JSON.stringify(data)}),
  retryTrial: id => api(base + '/trials/' + id + '/retry', {method: 'POST'}),
};
